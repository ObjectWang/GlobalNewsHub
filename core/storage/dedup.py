"""Duplicate detection for articles (PRD task P1.2).

The dedup anchor is ``source_url``, which carries a UNIQUE constraint
in the schema (section 2.3).
"""

from __future__ import annotations

import logging

from core.storage.database import Database

logger = logging.getLogger(__name__)


def find_article_id_by_url(db: Database, source_url: str) -> str | None:
    """Return the id of the article stored under ``source_url``, or None."""
    with db.session() as conn:
        row = conn.execute(
            "SELECT id FROM articles WHERE source_url = ?", (source_url,)
        ).fetchone()
    return str(row["id"]) if row is not None else None


def is_duplicate(db: Database, source_url: str) -> bool:
    """Return True when an article with ``source_url`` already exists."""
    return find_article_id_by_url(db, source_url) is not None
