"""News detail pane (PRD task P4.4).

QTextBrowser renders the article body as rich text, always shows the
mandatory source attribution line (来源标注: media + original link +
category/region labels), and opens external links in the system browser
(clickable-link acceptance).

Consumed by ``main.py`` (P4.8): the window's ``article_selected`` id is
resolved to an :class:`~core.models.Article` and passed to
:meth:`NewsDetailWidget.show_article`.
"""

from __future__ import annotations

import html
from typing import Final

from PySide6.QtWidgets import QTextBrowser, QVBoxLayout, QWidget

from core.constants import CATEGORIES, REGIONS
from core.models import Article

_CATEGORY_LABELS: Final[dict[str, str]] = {
    "politics": "时政",
    "economy": "财经",
    "military": "军事",
    "life": "生活",
    "tech": "科技",
    "other": "其他",
}

_REGION_LABELS: Final[dict[str, str]] = {
    "china": "中国",
    "us": "美国",
    "eu": "欧盟",
    "asia": "亚洲",
    "me": "中东",
    "global": "国际",
    "unknown": "未知",
}

_PLACEHOLDER: Final = "<p style='color:gray;'>选择左侧新闻以阅读详情…</p>"

_TEMPLATE: Final = """
<h2 style="font-size:19px; margin:2px 0 10px 0; line-height:140%;">{title}</h2>
<p style="color:#8a8f96; font-size:12px; margin:0 0 6px 0;">
来源：{source} · {published} · 栏目：{category} · 地区：{region}
</p>
<hr/>
<div style="font-size:14px; line-height:160%;">{body}</div>
<p><a href="{url}">阅读原文（{source}）</a></p>
"""


class NewsDetailWidget(QWidget):
    """Right-hand pane showing one article (or a placeholder)."""

    def __init__(self) -> None:
        super().__init__()
        self._current_url: str | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        self._browser = QTextBrowser(self)
        self._browser.setOpenExternalLinks(True)
        self._browser.setHtml(_PLACEHOLDER)
        layout.addWidget(self._browser)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def show_article(self, article: Article | None) -> None:
        """Render ``article``; ``None`` restores the placeholder."""
        if article is None:
            self._current_url = None
            self._browser.setHtml(_PLACEHOLDER)
            return
        self._current_url = article.source_url
        if article is None:
            self._browser.setHtml(_PLACEHOLDER)
            return
        self._browser.setHtml(
            _TEMPLATE.format(
                title=html.escape(article.title),
                source=html.escape(article.source_media),
                published=(article.published_at or article.fetched_at)
                .replace("T", " ")[:16],
                category=_CATEGORY_LABELS.get(article.category, article.category)
                if article.category in CATEGORIES
                else article.category,
                region=_REGION_LABELS.get(article.region, article.region)
                if article.region in REGIONS
                else article.region,
                body=self._render_body(article),
                url=html.escape(article.source_url, quote=True),
            )
        )

    def clear(self) -> None:
        """Reset to the placeholder state."""
        self.show_article(None)

    def to_plain_text(self) -> str:
        """Visible rendered text (test/introspection helper)."""
        return self._browser.toPlainText()

    def to_html(self) -> str:
        """Current HTML document source (test helper)."""
        return self._browser.toHtml()

    def original_url(self) -> str | None:
        """The 阅读原文 anchor target, or None in placeholder state."""
        return self._current_url

    def has_anchor(self, url: str) -> bool:
        """Whether the rendered document contains an <a href=url>."""
        return f'<a href="{url}"' in self.to_html()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _render_body(article: Article) -> str:
        """HTML body from content, falling back to escaped summary."""
        content = article.content
        if not content:
            content = article.summary or ""
        if not content:
            return "<p style='color:gray;'>(正文缺失)</p>"
        stripped = content.lstrip()
        looks_like_html = stripped.startswith("<")
        if looks_like_html:
            return content  # QTextBrowser renders a safe subset; no JS runs
        return (
            f"<p style='white-space:pre-wrap;'>{html.escape(content)}</p>"
        )
