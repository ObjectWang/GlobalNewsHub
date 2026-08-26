"""Region classification with a three-tier strategy (PRD P3.5, section 3.2 Step 3).

Priority order (fixed by the PRD):

1. **Domain mapping** — ``source_media`` matched against a preset
   media-name/domain table (e.g. 新华社 -> china, BBC -> eu). Longest
   key wins so compound names resolve before their substrings.
2. **NER dictionary** — geo place-name counting over title + summary;
   the region with the most mentions wins (ties broken by REGIONS
   declaration order for determinism).
3. **BERT assist** — only when both steps above fail and an auxiliary
   region model is wired in; its answer is trusted only at or above
   CONFIDENCE_THRESHOLD_HIGH.

Anything unresolved yields ``unknown``. Pure offline heuristics for
tiers 1-2: no network, no model file required.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

from core.classifier.bert_classifier import BertClassifier
from core.constants import CONFIDENCE_THRESHOLD_HIGH, REGIONS

logger = logging.getLogger(__name__)

RegionSource = Literal["domain", "ner", "model", "none"]

# source_media substrings (lowercased) -> region; longest key wins.
DEFAULT_DOMAIN_REGION_MAP: Final[dict[str, str]] = {
    # china
    "新华社": "china", "xinhua": "china", "人民日报": "china",
    "people.cn": "china", "央视": "china", "cctv": "china",
    "中国日报": "china", "chinadaily": "china", "环球时报": "china",
    "globaltimes": "china", "huanqiu": "china", "澎湃": "china",
    "thepaper": "china", "中新": "china", "光明日报": "china",
    "south china morning post": "china", "scmp": "china",
    # us
    "cnn": "us", "nytimes": "us", "new york times": "us",
    "washington post": "us", "wall street journal": "us", "wsj": "us",
    "foxnews": "us", "fox news": "us", "npr": "us",
    "associated press": "us", "ap news": "us", "bloomberg": "us",
    "politico": "us", "usatoday": "us", "usa today": "us",
    # eu
    "bbc": "eu", "reuters": "eu", "路透": "eu", "guardian": "eu",
    "金融时报": "eu", "financial times": "eu", "ft.com": "eu",
    "deutsche welle": "eu", "dw.com": "eu", "euronews": "eu",
    "france24": "eu", "le monde": "eu", "spiegel": "eu", "rfi": "eu",
    "el pais": "eu", "corriere": "eu", "economist": "eu",
    # asia
    "nhk": "asia", "asahi": "asia", "朝日新闻": "asia",
    "yomiuri": "asia", "读卖新闻": "asia", "mainichi": "asia",
    "japan times": "asia", "nikkei": "asia", "日经": "asia",
    "yonhap": "asia", "韩联社": "asia", "chosun": "asia",
    "korea herald": "asia", "channel newsasia": "asia",
    "straitstimes": "asia", "straits times": "asia",
    "bangkok post": "asia", "nikkei asia": "asia",
    # me
    "al jazeera": "me", "半岛电视台": "me", "jerusalem post": "me",
    "haaretz": "me", "times of israel": "me", "middle east eye": "me",
    "arab news": "me", "gulf news": "me", "khaleej times": "me",
}

# region -> geo place names counted over the article text (tier 2).
DEFAULT_GEO_KEYWORDS: Final[dict[str, tuple[str, ...]]] = {
    "china": (
        "中国", "北京", "上海", "香港", "台湾", "台北", "澳门", "深圳",
        "广州", "新疆", "西藏", "海南", "杭州", "南京", "成都", "武汉",
    ),
    "us": (
        "美国", "华盛顿", "纽约", "白宫", "加州", "得克萨斯", "五角大楼",
        "洛杉矶", "硅谷", "美联储大楼", "united states", "washington",
    ),
    "eu": (
        "英国", "伦敦", "法国", "巴黎", "德国", "柏林", "欧盟", "布鲁塞尔",
        "意大利", "罗马", "西班牙", "马德里", "北约总部", "欧洲议会",
        "britain", "france", "germany",
    ),
    "asia": (
        "日本", "东京", "韩国", "首尔", "朝鲜", "平壤", "印度", "新德里",
        "新加坡", "越南", "河内", "泰国", "曼谷", "东盟", "东京奥运会",
        "japan", "korea", "india",
    ),
    "me": (
        "以色列", "特拉维夫", "巴勒斯坦", "加沙", "约旦河西岸", "伊朗",
        "德黑兰", "伊拉克", "叙利亚", "大马士革", "沙特", "利雅得",
        "阿联酋", "迪拜", "黎巴嫩", "也门", "卡塔尔", "多哈", "中东",
    ),
    "global": (
        "联合国", "全球", "国际货币基金组织", "世界银行", "世贸组织",
        "g20", "g7", "达沃斯", "united nations",
    ),
}


@dataclass(frozen=True)
class RegionDecision:
    """Outcome of the three-tier region strategy."""

    region: str
    decided_by: RegionSource
    matched_term: str | None = None
    confidence: float | None = None


class RegionClassifier:
    """Classify article region via domain map -> geo dictionary -> model."""

    def __init__(
        self,
        domain_map: Mapping[str, str] | None = None,
        geo_keywords: Mapping[str, tuple[str, ...]] | None = None,
        model: BertClassifier | None = None,
    ) -> None:
        self._domain_map = dict(DEFAULT_DOMAIN_REGION_MAP if domain_map is None
                                else domain_map)
        self._geo = {region: tuple(terms)
                     for region, terms in
                     (DEFAULT_GEO_KEYWORDS if geo_keywords is None
                      else geo_keywords).items()}
        self._model = model

    def classify(self, source_media: str, text: str = "") -> RegionDecision:
        """Run the fixed priority chain; never raises, never blocks long."""
        domain = self._classify_by_domain(source_media)
        if domain is not None:
            return domain
        ner = self._classify_by_geo(text)
        if ner is not None:
            return ner
        return self._classify_by_model(text)

    def _classify_by_domain(self, source_media: str) -> RegionDecision | None:
        needle = (source_media or "").strip().lower()
        if not needle:
            return None
        best_key = ""
        best_region: str | None = None
        for key, region in self._domain_map.items():
            lowered_key = key.lower()
            if lowered_key in needle and len(lowered_key) > len(best_key):
                best_key = lowered_key
                best_region = region
        if best_region is not None:
            return RegionDecision(best_region, "domain", best_key)
        return None

    def _classify_by_geo(self, text: str) -> RegionDecision | None:
        if not text:
            return None
        lowered = text.lower()
        best_region: str | None = None
        best_term: str | None = None
        best_count = 0
        for region in REGIONS:
            for term in self._geo.get(region, ()):
                count = lowered.count(term.lower())
                # First-seen region wins ties => deterministic by REGIONS.
                if count > best_count:
                    best_count = count
                    best_region = region
                    best_term = term
        if best_region is not None and best_term is not None:
            return RegionDecision(best_region, "ner", best_term,
                                  float(best_count))
        return None

    def _classify_by_model(self, text: str) -> RegionDecision:
        if self._model is None or not text:
            return RegionDecision("unknown", "none")
        label, confidence = self._model.classify(text)
        if confidence >= CONFIDENCE_THRESHOLD_HIGH and label in REGIONS \
                and label != "unknown":
            return RegionDecision(label, "model", None, confidence)
        return RegionDecision("unknown", "none")
