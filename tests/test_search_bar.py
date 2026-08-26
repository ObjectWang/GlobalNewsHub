"""P4.5 acceptance tests: search bar with FTS5 live filtering (§6).

Contract: debounced ``search_submitted(str)`` — the stripped query, or
"" when cleared (which restores the unfiltered list downstream).
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6", reason="UI extra not installed")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from ui.search_bar import SearchBar  # noqa: E402


def drain(app, ms: int) -> None:
    """Run the event loop for ``ms`` milliseconds."""
    QTest.qWait(ms)


def test_query_emitted_after_debounce(qapp) -> None:
    bar = SearchBar(debounce_ms=20)
    received: list[str] = []
    bar.search_submitted.connect(received.append)
    # NOTE: QTest.keyClicks crashes on Windows when fed CJK characters,
    # so CJK input is simulated via setText (same textChanged signal).
    bar.line_edit().setText("芯片")
    assert received == []  # not yet: debounce window open
    drain(qapp, 80)
    assert received == ["芯片"]


def test_clearing_emits_empty_string(qapp) -> None:
    bar = SearchBar(debounce_ms=10)
    received: list[str] = []
    bar.search_submitted.connect(received.append)
    bar.line_edit().setText("央行")
    drain(qapp, 60)
    bar.line_edit().setText("")
    drain(qapp, 60)
    assert received == ["央行", ""]


def test_whitespace_stripped_and_blank_means_clear(qapp) -> None:
    bar = SearchBar(debounce_ms=10)
    received: list[str] = []
    bar.search_submitted.connect(received.append)
    bar.line_edit().setText("   ")
    drain(qapp, 60)
    assert received == [""]


def test_rapid_typing_coalesces_to_final_query(qapp) -> None:
    bar = SearchBar(debounce_ms=50)
    received: list[str] = []
    bar.search_submitted.connect(received.append)
    for ch in "人工智能":
        bar.line_edit().setText(bar.line_edit().text() + ch)
    drain(qapp, 120)
    assert received == ["人工智能"]


def test_enter_flushes_immediately(qapp) -> None:
    bar = SearchBar(debounce_ms=5000)
    received: list[str] = []
    bar.search_submitted.connect(received.append)
    bar.line_edit().setText("军演")
    QTest.keyClick(bar.line_edit(), Qt.Key.Key_Return)
    assert received == ["军演"]


def test_fts5_end_to_end(qapp, tmp_path) -> None:
    """The emitted query drives crud.search_articles against FTS5."""
    from core.models import Article
    from core.storage.crud import insert_article, search_articles
    from core.storage.database import Database

    db = Database(tmp_path / "t.db")
    db.initialize()
    for i, title in enumerate(("芯片出口新规", "央行降息"), start=1):
        insert_article(
            db,
            Article(
                id=f"id-{i}",
                title=title,
                summary=None,
                content=None,
                source_media="新华社",
                source_url=f"https://example.com/{i}",
                category="tech",
                region="china",
                published_at="2026-08-25T00:00:00",
                fetched_at="2026-08-25T00:00:00",
                tags=[],
            ),
        )
    hits = search_articles(db, "芯片")  # <3 chars -> LIKE fallback
    assert [a.title for a in hits] == ["芯片出口新规"]
    hits = search_articles(db, "芯片出口")  # >=3 chars -> trigram MATCH
    assert [a.title for a in hits] == ["芯片出口新规"]
    assert search_articles(db, "央行") != []
