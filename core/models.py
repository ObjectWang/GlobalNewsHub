"""Article entity model (PRD v3.0 section 2.1, field contract is strict)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Article:
    """News article stored in the ``articles`` table (section 2.3 DDL).

    ``tags`` and ``image_urls`` are JSON-serialized into TEXT columns;
    booleans are stored as INTEGER 0/1.
    """

    id: str                     # uuid4
    title: str                  # required
    summary: str | None         # abstract / lead paragraph
    content: str | None         # body HTML/Markdown
    source_media: str           # required, e.g. "新华社"
    source_url: str             # required, unique key (dedup anchor)
    category: str               # see CATEGORIES in core.constants
    region: str                 # see REGIONS in core.constants
    published_at: str | None    # ISO8601
    fetched_at: str             # ISO8601, required
    tags: list[str]             # JSON-serialized on storage
    language: str = "zh-CN"
    word_count: int = 0
    image_urls: list[str] = field(default_factory=list)
    is_paywalled: bool = False
    user_feedback: str | None = None
    is_favorite: bool = False
