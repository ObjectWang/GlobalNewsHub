"""P4.4 acceptance tests: news detail pane (PRD section 6).

Requirements: HTML rendering, explicit source attribution (来源标注),
and clickable original-article links.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6", reason="UI extra not installed")

from core.models import Article  # noqa: E402
from ui.news_detail import NewsDetailWidget  # noqa: E402


def make_article(**overrides: object) -> Article:
    base: dict[str, object] = dict(
        id="id-1",
        title="测试标题",
        summary="这是摘要",
        content="<p>正文第一段</p>",
        source_media="新华社",
        source_url="https://example.com/news/1",
        category="tech",
        region="china",
        published_at="2026-08-25T10:00:00",
        fetched_at="2026-08-25T11:00:00",
        tags=["科技"],
    )
    base.update(overrides)
    return Article(**base)  # type: ignore[arg-type]


def test_renders_title_and_source(qapp) -> None:
    view = NewsDetailWidget()
    view.show_article(make_article())
    text = view.to_plain_text()
    assert "测试标题" in text
    assert "新华社" in text  # 来源标注


def test_html_body_rendered_as_html(qapp) -> None:
    view = NewsDetailWidget()
    view.show_article(make_article(content="<p>正文第一段</p>"))
    # Raw markup must reach the renderer unescaped and display as text.
    assert "&lt;p&gt;" not in view.to_html()
    assert "正文第一段" in view.to_plain_text()


def test_plain_text_body_is_escaped(qapp) -> None:
    view = NewsDetailWidget()
    view.show_article(make_article(content="纯文本 <b>不是标签</b>"))
    assert "&lt;b&gt;" in view.to_html()
    assert "<b>不是标签" not in view.to_html()


def test_missing_content_falls_back_to_summary(qapp) -> None:
    view = NewsDetailWidget()
    view.show_article(make_article(content=None))
    assert "这是摘要" in view.to_plain_text()


def test_original_link_clickable(qapp) -> None:
    view = NewsDetailWidget()
    view.show_article(make_article())
    assert view.original_url() == "https://example.com/news/1"
    assert view._browser.openExternalLinks() is True or view.has_anchor(
        "https://example.com/news/1"
    )


def test_none_resets_to_placeholder(qapp) -> None:
    view = NewsDetailWidget()
    view.show_article(make_article())
    view.show_article(None)
    assert view.to_plain_text().strip() != ""
    assert view.original_url() is None


def test_category_region_labels_shown(qapp) -> None:
    view = NewsDetailWidget()
    view.show_article(make_article())
    text = view.to_plain_text()
    assert "科技" in text  # category label
    assert "中国" in text  # region label
