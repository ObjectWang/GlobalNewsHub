"""Tests for the classification pipeline (PRD tasks P3.4/P3.5).

Covers the eight acceptance cases of section 3.4 plus supporting unit
tests. The BERT session is replaced by a programmable fake so the
section 3.2 routing logic is verified exhaustively without model
artifacts; latency/memory gates activate automatically once the real
INT8 export exists under resources/models/.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from core.classifier.bert_classifier import BertClassifier
from core.classifier.keyword_fallback import KeywordRuleEngine
from core.classifier.pipeline import ClassificationPipeline
from core.constants import CATEGORIES
from core.models import Article

MODELS_DIR = Path(__file__).resolve().parents[1] / "resources" / "models"
REAL_MODEL = MODELS_DIR / "category_classifier_int8.onnx"
TOKENIZER_DIR = MODELS_DIR / "tokenizer"

real_model_available = pytest.mark.skipif(
    not REAL_MODEL.is_file() or REAL_MODEL.stat().st_size == 0,
    reason="real INT8 model not exported yet (P3.3 output)",
)


# ------------------------------------------------------------- fakes


def _logits_row(label_index: int, confidence: float) -> list[float]:
    """Logits whose softmax yields ``confidence`` on one label."""
    n = len(CATEGORIES)
    rest = (1.0 - confidence) / (n - 1)
    row = [0.0] * n
    row[label_index] = float(np.log(confidence / rest))
    return row


class FakeSession:
    """ONNX session stand-in popping pre-programmed logit rows."""

    def __init__(self, logits_rows: list[list[float]]) -> None:
        self._rows = list(logits_rows)

    def get_inputs(self) -> list[Any]:
        inputs: list[Any] = [type("I", (), {"name": name})()
                             for name in ("input_ids", "attention_mask",
                                          "token_type_ids")]
        return inputs

    def run(self, output_names: None, feed: Mapping[str, Any]) -> list[np.ndarray]:
        assert output_names is None
        row = self._rows.pop(0) if self._rows else [0.0] * len(CATEGORIES)
        return [np.array([row], dtype=np.float32)]


class FakeTokenizer:
    """Tokenizer stand-in producing constant short sequences."""

    def __call__(self, text: Any, text_pair: Any = None, **_kw: Any) -> (
        Mapping[str, Any]
    ):
        length = max(2, min(len(str(text)), 16))
        return {
            "input_ids": np.arange(length, dtype=np.int64).reshape(1, -1),
            "attention_mask": np.ones((1, length), dtype=np.int64),
            "token_type_ids": np.zeros((1, length), dtype=np.int64),
        }


def _classifier(confidence: float, label_index: int = 0) -> BertClassifier:
    return BertClassifier(
        session=FakeSession([_logits_row(label_index, confidence)]),
        tokenizer=FakeTokenizer(),  # type: ignore[arg-type]
        labels=CATEGORIES,
    )


def _pipeline(
    confidence: float,
    label_index: int = 0,
    rules: dict[str, tuple[str, ...]] | None = None,
) -> ClassificationPipeline:
    engine = KeywordRuleEngine(rules) if rules else KeywordRuleEngine()
    return ClassificationPipeline(_classifier(confidence, label_index), engine)


# --------------------------------------- section 3.2 routing (P3.4)


def test_high_confidence_uses_model() -> None:
    decision = _pipeline(0.92, label_index=4).classify_category("无关键词标题")
    assert decision.label == "tech"
    assert decision.decided_by == "model"
    assert decision.needs_review is False


def test_medium_confidence_keyword_override() -> None:
    # Model says tech @0.55 but economy keywords dominate the text.
    decision = _pipeline(0.55, label_index=4).classify_category(
        "央行发布货币政策", "股市与关税贸易战升级"
    )
    assert decision.label == "economy"
    assert decision.decided_by == "keyword"
    assert decision.needs_review is False


def test_medium_confidence_keyword_miss_keeps_model_flagged() -> None:
    decision = _pipeline(0.55, label_index=4).classify_category("无关键词标题")
    assert decision.label == "tech"
    assert decision.decided_by == "model"
    assert decision.needs_review is True


def test_low_confidence_fallback_to_keyword() -> None:
    decision = _pipeline(0.30, label_index=4).classify_category(
        "全国高考教育改革", "学校医院民生政策调整"
    )
    assert decision.label == "life"
    assert decision.decided_by == "keyword"


def test_keyword_miss_returns_other() -> None:
    decision = _pipeline(0.30, label_index=4).classify_category("无关键词标题")
    assert decision.label == "other"
    assert decision.needs_review is True


def test_pipeline_without_model_degrades_to_keywords() -> None:
    pipeline = ClassificationPipeline(None)
    hit = pipeline.classify_category("导弹军演", "国防部队演习")
    miss = pipeline.classify_category("无关键词标题")
    assert hit.decided_by == "keyword" and hit.label == "military"
    assert miss.label == "other" and miss.needs_review is True


def test_article_feedback_overrides_and_summary_fallback() -> None:
    article = Article(
        id="a", title="无关键词标题", summary=None, content=None,
        source_media="x", source_url="u", category="tech", region="unknown",
        published_at=None, fetched_at="2026-01-01T00:00:00+00:00", tags=[],
        user_feedback="economy",
    )
    decision = ClassificationPipeline(None).classify_article(article)
    assert decision.label == "economy"


# ------------------------------------- section 3.2 step 3 (P3.5)


def test_region_domain_mapping_priority() -> None:
    from core.classifier.region_classifier import RegionClassifier

    classifier = RegionClassifier()
    # US place names in text must lose against the 新华社 domain mapping.
    decision = classifier.classify("新华社", "华盛顿白宫与纽约国会山的报道")
    assert decision.region == "china"
    assert decision.decided_by == "domain"


def test_region_ner_fallback() -> None:
    from core.classifier.region_classifier import RegionClassifier

    classifier = RegionClassifier()
    decision = classifier.classify(
        "不知名通讯社", "东京与首尔的外长会谈涉及朝核问题"
    )
    assert decision.region == "asia"
    assert decision.decided_by == "ner"


# ------------------------------- real-artifact gates (section 3.4)


@real_model_available
def test_onnx_inference_latency_under_25ms() -> None:
    model = BertClassifier.from_files(REAL_MODEL, TOKENIZER_DIR)
    for _ in range(10):  # warm-up
        model.classify("中国股市发布新政策报告", "关税与贸易")
    import time

    start = time.perf_counter()
    runs = 50
    for _ in range(runs):
        model.classify("中国股市发布新政策报告", "关税与贸易")
    elapsed_ms = (time.perf_counter() - start) * 1000 / runs
    assert elapsed_ms < 25.0, f"average inference {elapsed_ms:.1f}ms >= 25ms"


def _rss_mb() -> float:
    if sys.platform == "win32":
        import ctypes

        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong),
                ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(counters)
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        ctypes.windll.psapi.GetProcessMemoryInfo(
            handle, ctypes.byref(counters), counters.cb
        )
        return counters.WorkingSetSize / (1024 * 1024)
    status = Path("/proc/self/status").read_text(encoding="utf-8")
    for line in status.splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) / 1024
    pytest.skip("cannot read process RSS on this platform")


@real_model_available
def test_model_memory_under_200mb() -> None:
    import onnxruntime  # noqa: F401  (baseline before measuring)
    import transformers  # noqa: F401

    before = _rss_mb()
    model = BertClassifier.from_files(REAL_MODEL, TOKENIZER_DIR)
    model.classify("中国股市发布新政策报告", "关税与贸易")
    growth = _rss_mb() - before
    assert growth < 200.0, f"classifier added {growth:.0f}MB >= 200MB"
