"""Keyword fallback rules for category classification (PRD section 3.2).

Used as the second opinion in the confidence routing: for medium model
confidence (0.5 <= c < 0.7) a hit overrides the model label; below the
low threshold (c < 0.5) keywords are the only signal left, and a miss
degrades the article to ``other`` with a review flag.

The rules are deliberately simple substring counts over title + summary:
deterministic, explainable, zero-dependency, and fast enough to run on
every article. A future YAML-driven rule pack can replace DEFAULT_
KEYWORD_RULES via the constructor without touching callers.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

# Ordered by CATEGORIES semantics; matching is case-insensitive on ASCII.
DEFAULT_KEYWORD_RULES: Final[dict[str, tuple[str, ...]]] = {
    "politics": (
        "政府", "国务院", "外交部", "选举", "政策", "领导人", "两会",
        "总统", "国会", "外交", "台海", "峰会", "制裁", "议会", "首相",
    ),
    "economy": (
        "经济", "金融", "股市", "央行", "GDP", "贸易", "关税", "美联储",
        "汇率", "投资", "楼市", "通胀", "财政", "货币", "上市公司",
    ),
    "military": (
        "军方", "军演", "导弹", "国防", "武器", "部队", "空军", "海军",
        "演习", "停火", "无人机", "核武", "维和", "军费",
    ),
    "life": (
        "教育", "医疗", "健康", "体育", "足球", "民生", "交通", "食品",
        "天气", "养老", "就业", "医院", "学校", "旅游", "房价民生",
    ),
    "tech": (
        "科技", "人工智能", "芯片", "互联网", "软件", "手机", "AI",
        "算法", "航天", "5G", "量子", "半导体", "机器人", "数据安全",
    ),
}


class KeywordRuleEngine:
    """Score every category by keyword hits; report the best or None."""

    def __init__(
        self, rules: Mapping[str, Sequence[str]] | None = None
    ) -> None:
        self._rules: dict[str, tuple[str, ...]] = {
            category: tuple(keywords)
            for category, keywords in (rules or DEFAULT_KEYWORD_RULES).items()
        }

    def match(self, text: str) -> str | None:
        """Return the highest-scoring category label, or None on no hit.

        Ties are broken deterministically by rule declaration order so
        the same text always yields the same label.
        """
        if not text:
            return None
        lowered = text.lower()
        best_label: str | None = None
        best_count = 0
        for label, keywords in self._rules.items():
            count = sum(lowered.count(keyword.lower()) for keyword in keywords)
            # Strictly-greater keeps earlier-declared labels winning ties.
            if count > best_count:
                best_count = count
                best_label = label
        return best_label if best_count > 0 else None
