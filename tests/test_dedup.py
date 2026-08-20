"""Tests for core.storage.dedup (PRD task P1.2).

Acceptance criterion: inserting a duplicate source_url returns the id
of the existing article instead of creating a new row.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.storage.crud import count_articles, insert_article
from core.storage.database import Database
from core.storage.dedup import find_article_id_by_url, is_duplicate
from tests.test_crud import make_article


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test.db")
    database.initialize()
    return database


def test_find_article_id_by_url_hit(db: Database) -> None:
    article = make_article()
    insert_article(db, article)
    assert find_article_id_by_url(db, article.source_url) == article.id


def test_find_article_id_by_url_miss(db: Database) -> None:
    assert find_article_id_by_url(db, "https://example.com/none") is None


def test_is_duplicate(db: Database) -> None:
    article = make_article()
    assert is_duplicate(db, article.source_url) is False
    insert_article(db, article)
    assert is_duplicate(db, article.source_url) is True


def test_duplicate_url_returns_existing_id(db: Database) -> None:
    """PRD acceptance: a repeated URL yields the existing article id."""
    first = make_article()
    second = make_article(source_url=first.source_url)  # same URL, new id
    assert insert_article(db, first) == first.id
    assert insert_article(db, second) == first.id
    assert count_articles(db) == 1
