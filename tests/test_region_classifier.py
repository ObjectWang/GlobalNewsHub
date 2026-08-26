"""Tests for core/classifier/region_classifier.py (PRD task P3.5).

Acceptance: all three strategy tiers are unit-covered — domain mapping
priority, NER geo-dictionary fallback, and the BERT assist tier with its
confidence gate, plus determinism and custom-map injection.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest

from core.classifier.region_classifier import (
    DEFAULT_DOMAIN_REGION_MAP,
    DEFAULT_GEO_KEYWORDS,
    RegionClassifier,
)
from core.constants import REGIONS


class _FakeRegionSession:
    """ONNX session stand-in returning one fixed logits row."""

    def __init__(self, row: list[float]) -> None:
        self._row = row

    def get_inputs(self) -> list[Any]:
        return [type("I", (), {"name": n})()
                for n in ("input_ids", "attention_mask", "token_type_ids")]

    def run(self, output_names: None, feed: Any) -> list[np.ndarray]:
        assert output_names is None
        return [np.array([self._row], dtype=np.float32)]


class _FakeTokenizer:
    def __call__(self, text: Any, text_pair: Any = None, **_kw: Any) -> Any:
        length = max(2, min(len(str(text)), 16))
        return {
            "input_ids": np.arange(length, dtype=np.int64).reshape(1, -1),
            "attention_mask": np.ones((1, length), dtype=np.int64),
            "token_type_ids": np.zeros((1, length), dtype=np.int64),
        }


def _region_model(region_index: int, confidence: float) -> Any:
    from core.classifier.bert_classifier import BertClassifier

    rest = (1.0 - confidence) / (len(REGIONS) - 1)
    row = [0.0] * len(REGIONS)
    row[region_index] = float(np.log(confidence / rest))
    return BertClassifier(
        session=_FakeRegionSession(row),
        tokenizer=_FakeTokenizer(),  # type: ignore[arg-type]
        labels=REGIONS,
    )


# ------------------------------------------------------------- tier 1


def test_domain_map_hits_before_text_signals() -> None:
    classifier = RegionClassifier()
    decision = classifier.classify("BBC 中文网", "北京上海香港满篇中国字")
    assert decision.decided_by == "domain"
    assert decision.region == "eu"


def test_longest_domain_key_wins() -> None:
    # "reuters" alone -> eu; a hypothetical longer key must win over it.
    custom = RegionClassifier(domain_map={
        "reuters": "eu",
        "reuters japan bureau": "asia",
    })
    assert custom.classify("Reuters Japan Bureau").region == "asia"
    assert custom.classify("Reuters").region == "eu"


def test_case_insensitive_domain_matching() -> None:
    classifier = RegionClassifier()
    assert classifier.classify("MyLocalMirrorOf-NYTImes").region == "us"


def test_unknown_media_falls_through() -> None:
    classifier = RegionClassifier()
    assert classifier.classify("某不知名通讯社").decided_by == "none"


# ------------------------------------------------------------- tier 2


def test_geo_dictionary_counts_mentions() -> None:
    classifier = RegionClassifier()
    decision = classifier.classify(
        "某不知名通讯社", "伊朗与以色列在加沙周边的紧张局势持续，中东多方斡旋"
    )
    assert decision.decided_by == "ner"
    assert decision.region == "me"


def test_tie_breaks_by_regions_declaration_order() -> None:
    # One mention each for china ("中国") and us ("美国"): REGIONS declares
    # china first, so it wins deterministically.
    classifier = RegionClassifier()
    decision = classifier.classify("某不知名通讯社", "中美会谈：中国与美国代表握手")
    assert decision.region == "china"


def test_custom_geo_keywords_injected() -> None:
    classifier = RegionClassifier(geo_keywords={"eu": ("莱茵河",)})
    decision = classifier.classify("某通讯社", "莱茵河水位创新低")
    assert decision.region == "eu" and decision.matched_term == "莱茵河"


# ------------------------------------------------------------- tier 3


def test_model_assist_used_only_when_high_confidence() -> None:
    confident = RegionClassifier(model=_region_model(2, 0.9))   # index 2 = eu
    weak = RegionClassifier(model=_region_model(2, 0.4))
    assert confident.classify("x通讯社", "无地名文本").region == "eu"
    assert weak.classify("x通讯社", "无地名文本").region == "unknown"


def test_model_label_outside_regions_is_rejected() -> None:
    model = _region_model(len(REGIONS) - 1, 0.99)  # "unknown" label itself
    classifier = RegionClassifier(model=model)
    assert classifier.classify("x通讯社", "无地名文本").region == "unknown"


# ------------------------------------------------------------- hygiene


def test_every_mapped_region_and_dict_region_is_valid() -> None:
    valid = set(REGIONS)
    assert set(DEFAULT_DOMAIN_REGION_MAP.values()) <= valid
    assert set(DEFAULT_GEO_KEYWORDS.keys()) <= valid


def test_empty_inputs_yield_unknown_without_model() -> None:
    classifier = RegionClassifier()
    decision = classifier.classify("", "")
    assert decision.region == "unknown" and decision.decided_by == "none"


def test_real_int8_region_model_if_present() -> None:
    pytest.importorskip("onnxruntime")
    models_dir = Path(__file__).resolve().parents[1] / "resources" / "models"
    model_path = models_dir / "region_classifier_int8.onnx"
    if not model_path.is_file() or model_path.stat().st_size == 0:
        pytest.skip("real region INT8 model not exported yet")
    from core.classifier.bert_classifier import BertClassifier

    classifier = RegionClassifier(
        model=BertClassifier.from_files(
            model_path, models_dir / "tokenizer", labels=REGIONS
        )
    )
    decision = classifier.classify("某不知名通讯社", "无任何地名的中性文本")
    assert decision.region in REGIONS
