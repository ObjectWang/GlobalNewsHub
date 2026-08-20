"""Shared constants (PRD v3.0 section 2.2, values are strict)."""

from typing import Final

CATEGORIES: Final[tuple[str, ...]] = ("politics", "economy", "military", "life", "tech", "other")
REGIONS: Final[tuple[str, ...]] = ("china", "us", "eu", "asia", "me", "global", "unknown")

CONFIDENCE_THRESHOLD_HIGH: Final = 0.7  # >=: adopt the model label directly
CONFIDENCE_THRESHOLD_LOW: Final = 0.5   # <: degrade to keyword rules only
MAX_SEQ_LENGTH: Final = 128             # BERT input truncation length
FETCH_INTERVAL_SECONDS: Final = 3       # minimum interval between requests
MAX_RETRY: Final = 3                    # max retry attempts
REQUEST_TIMEOUT: Final = 10             # per-request timeout (seconds)
HEALTH_FAIL_THRESHOLD: Final = 3        # consecutive failures before auto-disable
