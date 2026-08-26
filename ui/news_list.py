"""Virtual-scrolling news list (PRD task P4.3).

QAbstractListModel + QListView keeps memory and paint cost O(viewport)
regardless of dataset size, satisfying §1.3 (1000 条 ≥ 30fps): only
visible rows are painted and no per-row QWidget is created.

Signal-slot contract (consumed by the detail pane P4.4):

- ``article_selected(str)``: article id of the activated row (click or
  double-click/Enter).
"""

from __future__ import annotations

from typing import Final

from PySide6.QtCore import (
    QAbstractListModel,
    QModelIndex,
    Qt,
    Signal,
)
from PySide6.QtGui import QFont, QFontMetrics
from PySide6.QtWidgets import QListView, QVBoxLayout, QWidget

from core.models import Article

_TITLE_ROLE: Final = Qt.ItemDataRole.UserRole + 1
_SUMMARY_ROLE: Final = _TITLE_ROLE + 1
_META_ROLE: Final = _TITLE_ROLE + 2
_ID_ROLE: Final = _TITLE_ROLE + 3

_MAX_SUMMARY_CHARS: Final = 60


class NewsListModel(QAbstractListModel):
    """Flat, append-friendly model over Article rows."""

    TitleRole = _TITLE_ROLE
    SummaryRole = _SUMMARY_ROLE
    MetaRole = _META_ROLE
    IdRole = _ID_ROLE

    def __init__(self) -> None:
        super().__init__()
        self._articles: list[Article] = []

    # ------------------------------------------------------------------
    # Population API (called from the UI thread only; data arrives via
    # signals from the background thread, never by direct DB access)
    # ------------------------------------------------------------------

    def set_articles(self, articles: list[Article]) -> None:
        """Replace all rows (filter/search change)."""
        self.beginResetModel()
        self._articles = list(articles)
        self.endResetModel()

    def append_articles(self, articles: list[Article]) -> None:
        """Append rows (scroll-triggered pagination)."""
        if not articles:
            return
        first = len(self._articles)
        last = first + len(articles) - 1
        self.beginInsertRows(QModelIndex(), first, last)
        self._articles.extend(articles)
        self.endInsertRows()

    def article_at(self, row: int) -> Article | None:
        """Return the article stored at ``row``, or None when out of range."""
        if 0 <= row < len(self._articles):
            return self._articles[row]
        return None

    def find_row(self, article_id: str) -> int:
        """Row index of ``article_id`` or -1 when absent."""
        for row, article in enumerate(self._articles):
            if article.id == article_id:
                return row
        return -1

    # ------------------------------------------------------------------
    # QAbstractListModel interface
    # ------------------------------------------------------------------

    def rowCount(self, parent: QModelIndex | None = None) -> int:  # noqa: N802
        """Number of list rows."""
        return 0 if parent is not None and parent.isValid() else len(self._articles)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> object:
        """Return role-specific content for one row."""
        row = index.row()
        if not index.isValid() or not 0 <= row < len(self._articles):
            return None
        article = self._articles[row]
        if role == Qt.ItemDataRole.DisplayRole:
            return self._display_text(article)
        if role == _TITLE_ROLE:
            return article.title
        if role == _SUMMARY_ROLE:
            summary = article.summary or ""
            return summary[:_MAX_SUMMARY_CHARS] + ("…" if len(summary) > _MAX_SUMMARY_CHARS else "")
        if role == _META_ROLE:
            return self._meta_text(article)
        if role == _ID_ROLE:
            return article.id
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:  # type: ignore[override]
        """Rows are enabled and selectable."""
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    # ------------------------------------------------------------------
    # Display helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _display_text(article: Article) -> str:
        meta = NewsListModel._meta_text(article)
        return f"{article.title}\n{meta}"

    @staticmethod
    def _meta_text(article: Article) -> str:
        published = (article.published_at or article.fetched_at).replace("T", " ")
        return f"{article.source_media} · {published[:16]}"


class NewsListWidget(QWidget):
    """List pane widget wrapping :class:`NewsListModel`."""

    article_selected = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._model = NewsListModel()
        self._view = QListView(self)
        self._view.setModel(self._model)
        self._view.setUniformItemSizes(True)
        self._view.setWordWrap(False)
        self._view.setSelectionMode(QListView.SelectionMode.SingleSelection)
        self._view.setEditTriggers(QListView.EditTrigger.NoEditTriggers)
        self._view.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self._view.clicked.connect(self._emit_current)
        self._view.activated.connect(lambda _: self._emit_current())
        font = QFont()
        font.setPointSize(10)
        self._view.setFont(font)
        metrics_height = QFontMetrics(font).height() * 3 + 8
        self._view.setSpacing(2)
        self.setStyleSheet(f"QListView::item {{ height: {metrics_height}px; }}")

        layout.addWidget(self._view)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def model(self) -> NewsListModel:
        """Expose the backing model (for tests and pagination wiring)."""
        return self._model

    def set_articles(self, articles: list[Article]) -> None:
        """Replace all visible articles."""
        self._model.set_articles(articles)

    def append_articles(self, articles: list[Article]) -> None:
        """Extend the visible articles (infinite scroll)."""
        self._model.append_articles(articles)

    def selected_article_id(self) -> str | None:
        """Id of the current row, or None when nothing is selected."""
        index = self._view.currentIndex()
        if not index.isValid():
            return None
        value = self._model.data(index, _ID_ROLE)
        return str(value) if value else None

    def activate_row(self, article_id: str) -> None:
        """Programmatically select a row and emit its id (test helper)."""
        row = self._model.find_row(article_id)
        if row < 0:
            raise ValueError(f"unknown article id {article_id!r}")
        self._view.setCurrentIndex(self._model.index(row, 0))
        self.article_selected.emit(article_id)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _emit_current(self) -> None:
        article_id = self.selected_article_id()
        if article_id is not None:
            self.article_selected.emit(article_id)
