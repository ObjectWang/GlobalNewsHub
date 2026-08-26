"""Classification package.

BERT ONNX inference, keyword fallback rules, region classification
and the confidence-routed pipeline defined in §3.2.
"""

from core.classifier.bert_classifier import BertClassifier
from core.classifier.keyword_fallback import KeywordRuleEngine
from core.classifier.pipeline import CategoryDecision, ClassificationPipeline
from core.classifier.region_classifier import RegionClassifier

__all__ = [
    "BertClassifier",
    "CategoryDecision",
    "ClassificationPipeline",
    "KeywordRuleEngine",
    "RegionClassifier",
]
