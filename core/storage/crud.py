"""CRUD operations for articles (PRD task P1.2).

- ``insert_article`` dedups by ``source_url``: a repeated URL returns the
  id of the existing row instead of creating a new one (acceptance).
- The FTS5 index (``articles_fts``, external content per section 2.3) is
  maintained manually on insert/update/delete. On update and delete the
  FTS row is removed *before* the content row changes, so FTS5 can still
  read the old tokens from the content table.
"""

from __future__ import annotations

import json
import logging
import sqlite3

from core.models import Article
from core.storage.database import Database
from core.storage.dedup import find_article_id_by_url

logger = logging.getLogger(__name__)

_INSERT_SQL = (
    "INSERT INTO articles (id, title, summary, content, source_media, source_url,"
    " category, region, published_at, fetched_at, tags, language, word_count,"
    " image_urls, is_paywalled, user_feedback, is_favorite)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

_UPDATE_SQL = (
    "UPDATE articles SET title=?, summary=?, content=?, source_media=?, source_url=?,"
    " category=?, region=?, published_at=?, fetched_at=?, tags=?, language=?,"
    " word_count=?, image_urls=?, is_paywalled=?, user_feedback=?, is_favorite=?"
    " WHERE id=?"
)

_FTS_INSERT_SQL = (
    "INSERT INTO articles_fts(rowid, title, summary, content) VALUES (?, ?, ?, ?)"
)


def _article_to_params(article: Article) -> tuple[object, ...]:
    """Serialize an Article into an ``articles`` row parameter tuple."""
    return (
        article.id,
        article.title,
        article.summary,
        article.content,
        article.source_media,
        article.source_url,
        article.category,
        article.region,
        article.published_at,
        article.fetched_at,
        json.dumps(article.tags, ensure_ascii=False),
        article.language,
        article.word_count,
        json.dumps(article.image_urls, ensure_ascii=False),
        int(article.is_paywalled),
        article.user_feedback,
        int(article.is_favorite),
    )


def _row_to_article(row: sqlite3.Row) -> Article:
    """Deserialize an ``articles`` row back into an Article."""
    return Article(
        id=row["id"],
        title=row["title"],
        summary=row["summary"],
        content=row["content"],
        source_media=row["source_media"],
        source_url=row["source_url"],
        category=row["category"],
        region=row["region"],
        published_at=row["published_at"],
        fetched_at=row["fetched_at"],
        tags=json.loads(row["tags"]) if row["tags"] else [],
        language=row["language"],
        word_count=row["word_count"],
        image_urls=json.loads(row["image_urls"]) if row["image_urls"] else [],
        is_paywalled=bool(row["is_paywalled"]),
        user_feedback=row["user_feedback"],
        is_favorite=bool(row["is_favorite"]),
    )


def insert_article(db: Database, article: Article) -> str:
    """Insert an article and index it in FTS; return the article id.

    If ``article.source_url`` already exists, return the existing id
    without writing a new row (dedup acceptance criterion).
    """
    existing_id = find_article_id_by_url(db, article.source_url)
    if existing_id is not None:
        logger.info("Duplicate URL %s -> existing id %s", article.source_url, existing_id)
        return existing_id
    try:
        with db.session() as conn:
            cursor = conn.execute(_INSERT_SQL, _article_to_params(article))
            conn.execute(
                _FTS_INSERT_SQL,
                (cursor.lastrowid, article.title, article.summary, article.content),
            )
    except sqlite3.IntegrityError:
        # A concurrent insert of the same URL won the race.
        existing_id = find_article_id_by_url(db, article.source_url)
        if existing_id is None:
            raise
        logger.info("Duplicate URL %s -> existing id %s", article.source_url, existing_id)
        return existing_id
    return article.id


def get_article(db: Database, article_id: str) -> Article | None:
    """Return the article with ``article_id``, or None when absent."""
    with db.session() as conn:
        row = conn.execute("SELECT * FROM articles WHERE id = ?", (article_id,)).fetchone()
    return _row_to_article(row) if row is not None else None


def update_article(db: Database, article: Article) -> bool:
    """Replace every field of an existing article; True when a row matched."""
    with db.session() as conn:
        row = conn.execute(
            "SELECT rowid FROM articles WHERE id = ?", (article.id,)
        ).fetchone()
        if row is None:
            return False
        rowid = row["rowid"]
        conn.execute("DELETE FROM articles_fts WHERE rowid = ?", (rowid,))
        conn.execute(_UPDATE_SQL, _article_to_params(article)[1:] + (article.id,))
        conn.execute(
            _FTS_INSERT_SQL, (rowid, article.title, article.summary, article.content)
        )
    return True


def delete_article(db: Database, article_id: str) -> bool:
    """Delete an article and its FTS index row; True when a row matched."""
    with db.session() as conn:
        row = conn.execute(
            "SELECT rowid FROM articles WHERE id = ?", (article_id,)
        ).fetchone()
        if row is None:
            return False
        conn.execute("DELETE FROM articles_fts WHERE rowid = ?", (row["rowid"],))
        conn.execute("DELETE FROM articles WHERE id = ?", (article_id,))
    return True


def list_articles(
    db: Database,
    *,
    category: str | None = None,
    region: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[Article]:
    """List articles, newest first, with optional category/region filters."""
    query = "SELECT * FROM articles"
    clauses: list[str] = []
    params: list[object] = []
    if category is not None:
        clauses.append("category = ?")
        params.append(category)
    if region is not None:
        clauses.append("region = ?")
        params.append(region)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY published_at DESC, rowid DESC LIMIT ? OFFSET ?"
    params.extend((limit, offset))
    with db.session() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_row_to_article(row) for row in rows]


def search_articles(db: Database, query: str, *, limit: int = 200) -> list[Article]:
    """Full-text search over title/summary/content.

    Uses FTS5 MATCH (trigram tokenizer since migration 002), which
    supports CJK substring matching for queries of >= 3 characters.
    Shorter queries and MATCH-syntax failures fall back to a LIKE scan,
    so a 2-character Chinese query such as "芯片" still finds results.
    """
    needle = query.strip()
    if not needle:
        return []
    if _needs_like_fallback(needle):
        return _search_articles_like(db, needle, limit)
    try:
        with db.session() as conn:
            rows = conn.execute(
                "SELECT a.* FROM articles AS a"
                " JOIN articles_fts ON articles_fts.rowid = a.rowid"
                " WHERE articles_fts MATCH ? ORDER BY rank LIMIT ?",
                (f'"{needle.replace(chr(34), chr(34) * 2)}"', limit),
            ).fetchall()
        return [_row_to_article(row) for row in rows]
    except sqlite3.OperationalError:
        logger.info("FTS MATCH failed for %r; falling back to LIKE", query)
        return _search_articles_like(db, needle, limit)


_CJK_RANGE = (
    (0x4E00, 0x9FFF),   # CJK unified ideographs
    (0x3400, 0x4DBF),   # extension A
    (0xF900, 0xFAFF),   # compatibility ideographs
)


def _contains_cjk(text: str) -> bool:
    """Whether ``text`` contains any CJK ideograph."""
    return any(
        lo <= ord(char) <= hi for char in text for lo, hi in _CJK_RANGE
    )


def _needs_like_fallback(needle: str) -> bool:
    """LIKE is required for sub-trigram-length queries containing CJK."""
    return len(needle) < 3 and _contains_cjk(needle)


def _search_articles_like(db: Database, needle: str, limit: int) -> list[Article]:
    """Substring scan fallback (no index; fine at local-database scale)."""
    pattern = f"%{needle}%"
    with db.session() as conn:
        rows = conn.execute(
            "SELECT * FROM articles"
            " WHERE title LIKE ? OR summary LIKE ? OR content LIKE ?"
            " ORDER BY published_at DESC, rowid DESC LIMIT ?",
            (pattern, pattern, pattern, limit),
        ).fetchall()
    return [_row_to_article(row) for row in rows]


def count_articles(
    db: Database, *, category: str | None = None, region: str | None = None
) -> int:
    """Count articles with optional category/region filters."""
    query = "SELECT COUNT(*) FROM articles"
    clauses: list[str] = []
    params: list[object] = []
    if category is not None:
        clauses.append("category = ?")
        params.append(category)
    if region is not None:
        clauses.append("region = ?")
        params.append(region)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    with db.session() as conn:
        count = conn.execute(query, params).fetchone()[0]
    return int(count)
