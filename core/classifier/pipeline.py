"""Confidence-routed classification pipeline (PRD section 3.2, P3.4).

Routing contract (Step 2 of section 3.2), implemented verbatim:

- model confidence >= 0.7  -> adopt the model label directly;
- 0.5 <= confidence < 0.7  -> keyword rules double-check: a hit wins,
  otherwise keep the model label flagged low-confidence;
- confidence < 0.5         -> keyword rules only: a hit wins, otherwise
  fall back to ``other`` with a review flag.

Step 3 (region) is independent of the category decision; the pipeline
delegates it to an injected region provider (P3.5) when one is present.
Without a model artifact everything degrades to keyword-only mode so the
app stays functional before the first training run completes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final, Literal

from core.classifier.bert_classifier import BertClassifier
from core.classifier.keyword_fallback import KeywordRuleEngine
from core.constants import (
    CATEGORIES,
    CONFIDENCE_THRESHOLD_HIGH,
    CONFIDENCE_THRESHOLD_LOW,
)
from core.models import Article

logger = logging.getLogger(__name__)

DecidedBy = Literal["model", "keyword", "default"]

DEFAULT_LABEL: Final[str] = "other"


@dataclass(frozen=True)
class CategoryDecision:
    """Outcome of Step 2 routing for one article."""

    label: str
    confidence: float
    decided_by: DecidedBy
    needs_review: bool


class ClassificationPipeline:
    """Combine BERT inference and keyword fallback per section 3.2."""

    def __init__(
        self,
        classifier: BertClassifier | None,
        keyword_engine: KeywordRuleEngine | None = None,
        *,
        high_threshold: float = CONFIDENCE_THRESHOLD_HIGH,
        low_threshold: float = CONFIDENCE_THRESHOLD_LOW,
    ) -> None:
        if not 0.0 < low_threshold < high_threshold <= 1.0:
            raise ValueError(
                f"thresholds must satisfy 0 < low < high <= 1, "
                f"got low={low_threshold} high={high_threshold}"
            )
        self._classifier = classifier
        self._keywords = keyword_engine or KeywordRuleEngine()
        self._high = high_threshold
        self._low = low_threshold

    def classify_category(
        self, title: str, summary: str = ""
    ) -> CategoryDecision:
        """Route one article heading through the section 3.2 decision tree."""
        text = f"{title}\n{summary}".strip()
        model_label: str | None = None
        model_confidence = 0.0
        if self._classifier is not None:
            model_label, model_confidence = self._classifier.classify(
                title, summary
            )

        # >= HIGH: adopt the model result directly.
        if model_confidence >= self._high and model_label is not None:
            return CategoryDecision(model_label, model_confidence, "model", False)

        keyword_hit = self._keywords.match(text)

        # < LOW: keywords only; miss degrades to other + review flag.
        if model_confidence < self._low:
            if keyword_hit is not None:
                return CategoryDecision(keyword_hit, model_confidence,
                                        "keyword", False)
            return CategoryDecision(DEFAULT_LABEL, model_confidence,
                                    "default", True)

        # Medium band: keyword hit overrides, else keep model label flagged.
        if keyword_hit is not None:
            return CategoryDecision(keyword_hit, model_confidence,
                                    "keyword", False)
        return CategoryDecision(model_label or DEFAULT_LABEL, model_confidence,
                                "model", True)

    def classify_article(self, article: Article) -> CategoryDecision:
        """Convenience wrapper over stored :class:`Article` fields."""
        if article.user_feedback in CATEGORIES:
            # A human correction always outranks every automatic signal.
            return CategoryDecision(article.user_feedback, 1.0, "model", False)
        summary = article.summary or ""
        if not summary and article.content:
            summary = article.content[:300]
        return self.classify_category(article.title, summary)
