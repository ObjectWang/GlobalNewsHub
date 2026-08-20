"""Tests for core.storage.database (PRD task P1.1).

Acceptance: schema strictly matches the section 2.3 DDL and WAL mode
is active on every connection.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from core.storage.database import MIGRATIONS_DIR, Database

EXPECTED_ARTICLE_COLUMNS = [
    "id", "title", "summary", "content", "source_media", "source_url",
    "category", "region", "published_at", "fetched_at", "tags", "language",
    "word_count", "image_urls", "is_paywalled", "user_feedback", "is_favorite",
]
EXPECTED_SOURCE_HEALTH_COLUMNS = [
    "source_id", "name", "last_status", "fail_count", "last_fetch_at", "disabled",
]
EXPECTED_INDEXES = {"idx_category", "idx_region", "idx_published", "idx_source"}


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    """Provide an initialized database in a temp directory."""
    database = Database(tmp_path / "test.db")
    database.initialize()
    return database


def _insert_article(conn: sqlite3.Connection, *, article_id: str, url: str) -> None:
    conn.execute(
        "INSERT INTO articles (id, title, source_media, source_url, fetched_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (article_id, "title", "media", url, "2026-08-20T00:00:00+08:00"),
    )


def test_migration_script_exists() -> None:
    assert (MIGRATIONS_DIR / "001_init.sql").is_file()


def test_wal_mode_enabled(db: Database) -> None:
    with db.session() as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert str(mode).lower() == "wal"


def test_tables_created(db: Database) -> None:
    with db.session() as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    names = {row["name"] for row in rows}
    assert {"articles", "articles_fts", "source_health"} <= names


def test_articles_columns_match_contract(db: Database) -> None:
    with db.session() as conn:
        info = conn.execute("PRAGMA table_info(articles)").fetchall()
    assert [col["name"] for col in info] == EXPECTED_ARTICLE_COLUMNS
    by_name = {col["name"]: col for col in info}
    assert by_name["id"]["pk"] == 1
    for required in ("title", "source_media", "source_url", "category", "region", "fetched_at"):
        assert by_name[required]["notnull"] == 1
    assert by_name["category"]["dflt_value"] == "'other'"
    assert by_name["region"]["dflt_value"] == "'unknown'"
    assert by_name["language"]["dflt_value"] == "'zh-CN'"


def test_source_health_columns(db: Database) -> None:
    with db.session() as conn:
        info = conn.execute("PRAGMA table_info(source_health)").fetchall()
    assert [col["name"] for col in info] == EXPECTED_SOURCE_HEALTH_COLUMNS


def test_indexes_created(db: Database) -> None:
    with db.session() as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='articles'"
        ).fetchall()
    names = {row["name"] for row in rows}
    assert EXPECTED_INDEXES <= names
    # UNIQUE constraint on source_url materializes as an autoindex.
    assert any(name.startswith("sqlite_autoindex_") for name in names)


def test_source_url_unique(db: Database) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        with db.session() as conn:
            _insert_article(conn, article_id="a1", url="https://example.com/x")
            _insert_article(conn, article_id="a2", url="https://example.com/x")


def test_fts5_table_queryable(db: Database) -> None:
    with db.session() as conn:
        _insert_article(conn, article_id="a1", url="https://example.com/1")
        rowid = conn.execute("SELECT rowid FROM articles WHERE id='a1'").fetchone()[0]
        conn.execute(
            "INSERT INTO articles_fts(rowid, title, summary, content) VALUES (?, ?, ?, ?)",
            (rowid, "global economy grows", "summary", "content"),
        )
        hits = conn.execute(
            "SELECT rowid FROM articles_fts WHERE articles_fts MATCH ?", ("economy",)
        ).fetchall()
    assert [hit["rowid"] for hit in hits] == [rowid]


def test_initialize_is_idempotent(tmp_path: Path) -> None:
    database = Database(tmp_path / "t.db")
    database.initialize()
    database.initialize()


def test_session_commits_on_success(db: Database) -> None:
    with db.session() as conn:
        _insert_article(conn, article_id="a1", url="https://example.com/c")
    with db.session() as conn:
        row = conn.execute("SELECT id FROM articles WHERE id='a1'").fetchone()
    assert row is not None


def test_session_rolls_back_on_error(db: Database) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        with db.session() as conn:
            _insert_article(conn, article_id="a1", url="https://example.com/r1")
            _insert_article(conn, article_id="a1", url="https://example.com/r2")
    with db.session() as conn:
        count = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    assert count == 0
