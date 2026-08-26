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

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QTextBrowser, QVBoxLayout, QWidget

from core import translation
from core.constants import CATEGORIES, REGIONS
from core.models import Article
from core.utils.images import extract_image_urls

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
    """Right-hand pane showing one article (or a placeholder).

    Signals (requests #3/#4):
    - ``translate_requested(str article_id, str text)`` — user clicked the
      translate button on a non-Chinese article.
    - ``images_requested(str article_id, list urls)`` — current article
      carries remote images; controller should download & feed back via
      :meth:`apply_local_image`.
    """

    translate_requested = Signal(str, str)
    images_requested = Signal(str, list)

    def __init__(self) -> None:
        super().__init__()
        self._current_url: str | None = None
        self._current_id: str | None = None
        self._last_html: str = _PLACEHOLDER
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        top = QHBoxLayout()
        top.addStretch(1)
        self._translate_btn = QPushButton("翻译为中文", self)
        self._translate_btn.setVisible(False)
        self._translate_btn.clicked.connect(self._on_translate_clicked)
        top.addWidget(self._translate_btn)
        layout.addLayout(top)

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
            self._current_id = None
            self._set_html(_PLACEHOLDER)
            self._translate_btn.setVisible(False)
            return
        self._current_url = article.source_url
        self._current_id = article.id
        self._source_text_cache = translation.source_text(article)
        html_doc = _TEMPLATE.format(
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
        self._set_html(html_doc)
        self._translate_btn.setVisible(translation.needs_translation(article))
        image_urls = extract_image_urls(article.content or "")
        if image_urls:
            self.images_requested.emit(article.id, image_urls)

    def apply_local_image(self, article_id: str, src_url: str, local_path: str) -> None:
        """Register a downloaded image and re-render (request #4)."""
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QImage, QTextDocument

        if article_id != self._current_id:
            return
        image = QImage(local_path)
        if image.isNull():
            return
        self._browser.document().addResource(
            QTextDocument.ResourceType.ImageResource, QUrl(src_url), image
        )
        bar = self._browser.verticalScrollBar()
        pos = bar.value()
        self._browser.setHtml(self._last_html)
        bar.setValue(min(pos, bar.maximum()))

    def apply_translation(self, article_id: str, zh_text: str) -> None:
        """Prepend the translated paragraph above the original (#3)."""
        if article_id != self._current_id:
            return
        block = (
            '<div style="background:rgba(47,111,219,0.10);'
            'border-left:4px solid #2f6fdb; padding:8px 10px;'
            'border-radius:6px; margin-bottom:10px;">'
            f"{html.escape(zh_text)}</div>"
        )
        marker = '<hr/>'
        if marker in self._last_html:
            self._set_html(self._last_html.replace(marker, block + marker, 1))
        else:
            self._set_html(block + self._last_html)
        self._translate_btn.setEnabled(True)
        self._translate_btn.setText("已翻译")

    def set_translating(self, busy: bool) -> None:
        """Toggle the translate button while a request is in flight."""
        self._translate_btn.setEnabled(not busy)
        self._translate_btn.setText("翻译中…" if busy else "翻译为中文")

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

    def _set_html(self, html_text: str) -> None:
        """Remember ``html_text`` and render it (re-render keeps images)."""
        self._last_html = html_text
        self._browser.setHtml(html_text)

    def _on_translate_clicked(self) -> None:
        if self._current_id is None:
            return
        text = getattr(self, "_source_text_cache", "")
        if not text.strip():
            return
        self.set_translating(True)
        self.translate_requested.emit(self._current_id, text)

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
