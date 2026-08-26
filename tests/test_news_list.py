"""P4.3 acceptance tests: virtual-scrolling news list (PRD section 6).

Contract consumed by the detail pane (P4.4) and search bar (P4.5):
- ``article_selected(str)`` carries the article id of the activated row.
- ``set_articles`` replaces content on filter/search changes;
  ``append_articles`` extends it during scroll-triggered pagination.
"""

from __future__ import annotations

import time

import pytest

pytest.importorskip("PySide6", reason="UI extra not installed")

from PySide6.QtGui import QPixmap  # noqa: E402

from core.models import Article  # noqa: E402
from ui.news_list import NewsListModel, NewsListWidget  # noqa: E402


def make_article(i: int) -> Article:
    """A minimal valid article fixture."""
    return Article(
        id=f"id-{i}",
        title=f"标题{i}：示例新闻",
        summary=f"摘要{i}",
        content=None,
        source_media="新华社",
        source_url=f"https://example.com/{i}",
        category="tech",
        region="china",
        published_at=f"2026-08-{(i % 28) + 1:02d}T10:00:00",
        fetched_at="2026-08-25T10:00:00",
        tags=[],
    )


def test_model_row_count(qapp) -> None:
    model = NewsListModel()
    model.set_articles([make_article(i) for i in range(5)])
    assert model.rowCount() == 5


def test_display_role_returns_title(qapp) -> None:
    model = NewsListModel()
    model.set_articles([make_article(7)])
    assert model.data(model.index(0, 0), NewsListModel.TitleRole) == "标题7：示例新闻"


def test_activated_row_emits_article_id(qapp) -> None:
    view = NewsListWidget()
    received: list[str] = []
    view.article_selected.connect(received.append)
    view.set_articles([make_article(i) for i in range(3)])
    view.activate_row("id-1")
    assert received == ["id-1"]


def test_set_articles_replaces_content(qapp) -> None:
    view = NewsListWidget()
    view.set_articles([make_article(i) for i in range(10)])
    view.set_articles([make_article(99)])
    assert view.model().rowCount() == 1  # type: ignore[union-attr]


def test_append_articles_extends_content(qapp) -> None:
    view = NewsListWidget()
    view.set_articles([make_article(i) for i in range(2)])
    view.append_articles([make_article(i) for i in range(3)])
    assert view.model().rowCount() == 5  # type: ignore[union-attr]


def test_selected_article_id_roundtrip(qapp) -> None:
    view = NewsListWidget()
    view.set_articles([make_article(i) for i in range(3)])
    view.activate_row("id-2")
    assert view.selected_article_id() == "id-2"


def test_render_1000_rows_meets_30fps(qapp) -> None:
    """Acceptance §1.3: 列表帧率（1000 条）≥ 30fps.

    Proxy measurement: repaint the visible viewport 90 times with a
    populated 1000-row model; 90 paints must finish under 3 seconds.
    Model/view only paints visible rows, so this stays O(viewport).
    """
    view = NewsListWidget()
    view.resize(800, 600)
    view.show()
    try:
        view.set_articles([make_article(i) for i in range(1000)])
        pixmap = QPixmap(view.size())
        start = time.perf_counter()
        frames = 90
        for _ in range(frames):
            view.render(pixmap)
        elapsed = time.perf_counter() - start
        assert frames / elapsed >= 30.0, f"{frames} paints took {elapsed:.2f}s"
    finally:
        view.close()
