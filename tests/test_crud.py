"""Tests for core.storage.crud (PRD task P1.2)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from core.models import Article
from core.storage.crud import (
    count_articles,
    delete_article,
    get_article,
    insert_article,
    list_articles,
    search_articles,
    update_article,
)
from core.storage.database import Database


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test.db")
    database.initialize()
    return database


def make_article(**overrides: Any) -> Article:
    """Build a fully populated Article with a unique id and URL."""
    base: dict[str, Any] = {
        "id": uuid4().hex,
        "title": "China economy grows steadily",
        "summary": "a brief summary",
        "content": "full article content",
        "source_media": "新华社",
        "source_url": f"https://example.com/{uuid4().hex}",
        "category": "economy",
        "region": "china",
        "published_at": "2026-08-19T08:00:00+08:00",
        "fetched_at": "2026-08-20T01:00:00+08:00",
        "tags": ["经济", "中国"],
        "language": "zh-CN",
        "word_count": 500,
        "image_urls": ["https://example.com/a.jpg"],
        "is_paywalled": False,
        "user_feedback": None,
        "is_favorite": False,
    }
    base.update(overrides)
    return Article(**base)


def test_insert_and_get_roundtrip(db: Database) -> None:
    article = make_article()
    returned_id = insert_article(db, article)
    assert returned_id == article.id
    loaded = get_article(db, article.id)
    assert loaded is not None
    assert loaded == article


def test_get_missing_returns_none(db: Database) -> None:
    assert get_article(db, "no-such-id") is None


def test_update_article_and_fts_resync(db: Database) -> None:
    article = make_article()
    insert_article(db, article)
    updated = make_article(id=article.id, source_url=article.source_url,
                           title="military drill concluded", category="military")
    assert update_article(db, updated) is True
    loaded = get_article(db, article.id)
    assert loaded is not None
    assert loaded.title == "military drill concluded"
    assert loaded.category == "military"
    # FTS index follows the update: new title matches, old one does not.
    assert [a.id for a in search_articles(db, "military")] == [article.id]
    assert search_articles(db, "steadily") == []


def test_update_missing_returns_false(db: Database) -> None:
    assert update_article(db, make_article()) is False


def test_delete_article_and_fts_cleanup(db: Database) -> None:
    article = make_article()
    insert_article(db, article)
    assert delete_article(db, article.id) is True
    assert get_article(db, article.id) is None
    assert search_articles(db, "steadily") == []


def test_delete_missing_returns_false(db: Database) -> None:
    assert delete_article(db, "no-such-id") is False


def test_list_articles_filters_and_order(db: Database) -> None:
    old = make_article(category="tech", region="us",
                       published_at="2026-08-18T08:00:00+08:00")
    new = make_article(category="tech", region="china",
                       published_at="2026-08-19T08:00:00+08:00")
    other = make_article(category="life", region="china",
                         published_at="2026-08-17T08:00:00+08:00")
    for article in (old, new, other):
        insert_article(db, article)
    tech = list_articles(db, category="tech")
    assert [a.id for a in tech] == [new.id, old.id]  # published_at DESC
    assert [a.id for a in list_articles(db, region="china")] == [new.id, other.id]
    assert [a.id for a in list_articles(db, category="tech", region="us")] == [old.id]


def test_list_articles_limit_offset(db: Database) -> None:
    for i in range(5):
        insert_article(db, make_article(published_at=f"2026-08-1{i}T08:00:00+08:00"))
    page1 = list_articles(db, limit=2, offset=0)
    page2 = list_articles(db, limit=2, offset=2)
    assert len(page1) == 2 and len(page2) == 2
    assert {a.id for a in page1}.isdisjoint({a.id for a in page2})


def test_search_articles(db: Database) -> None:
    target = make_article(title="quantum computing breakthrough")
    insert_article(db, target)
    insert_article(db, make_article(title="local sports results"))
    assert [a.id for a in search_articles(db, "quantum")] == [target.id]


def test_count_articles(db: Database) -> None:
    insert_article(db, make_article(category="tech"))
    insert_article(db, make_article(category="life"))
    assert count_articles(db) == 2
    assert count_articles(db, category="tech") == 1
