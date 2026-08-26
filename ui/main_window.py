"""Main window skeleton (PRD task P4.1).

Three-pane shell with a strict signal-slot contract consumed by the
later Phase 4 tasks:

- ``refresh_requested``  : menu/toolbar/F5 -> background fetch thread (P4.8)
- ``settings_requested`` : menu action    -> settings dialog (P4.6)
- ``category_selected``  : sidebar filter -> list reload (P4.2)
- ``region_selected``    : sidebar filter -> list reload (P4.2)
- ``search_submitted``   : search box     -> FTS5 query (P4.5)
- ``article_selected``   : list row       -> detail pane (P4.3/P4.4)

The central widget is a QSplitter whose three panes are placeholder
labels behind ``QWidget`` containers; P4.2-P4.5 replace their content
via :meth:`replace_sidebar` / :meth:`replace_list` / :meth:`replace_detail`
without touching the layout skeleton.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QMainWindow,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

_WINDOW_TITLE: Final = "GlobalNewsHub"
_SIDEBAR_WIDTH: Final = 220
_LIST_WIDTH: Final = 480

_THEMES_DIR: Final = Path(__file__).resolve().parent / "themes"
_KNOWN_THEMES: Final[tuple[str, ...]] = ("light", "dark")


def load_theme_sheet(theme: str) -> str:
    """Return the QSS text for ``theme``; unknown names fall back to light."""
    name = theme if theme in _KNOWN_THEMES else "light"
    return (_THEMES_DIR / f"{name}.qss").read_text(encoding="utf-8")


def apply_theme(app: QApplication, theme: str) -> None:
    """Restyle the whole application instantly (P4.7 acceptance)."""
    app.setStyleSheet(load_theme_sheet(theme))


class MainWindow(QMainWindow):
    """Top-level window: menus, status bar and the three-pane splitter."""

    refresh_requested = Signal()
    settings_requested = Signal()
    settings_applied = Signal(dict)
    category_selected = Signal(str)
    region_selected = Signal(str)
    search_submitted = Signal(str)
    article_selected = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(_WINDOW_TITLE)
        self.resize(1280, 800)
        self.setMinimumSize(960, 620)
        self._settings_path: Path | None = None
        self._report_dialog: QWidget | None = None
        self._build_central()
        self._build_actions()
        self._build_menus()
        self._build_status_bar()

    # ------------------------------------------------------------------
    # Pane replacement API (used by P4.2-P4.5; placeholders until then)
    # ------------------------------------------------------------------

    @property
    def sidebar_slot(self) -> QWidget:
        """Left pane container holding the sidebar filter widget."""
        return self._sidebar_slot

    @property
    def list_slot(self) -> QWidget:
        """Center pane container holding the news list + search bar."""
        return self._list_slot

    @property
    def detail_slot(self) -> QWidget:
        """Right pane container holding the article detail view."""
        return self._detail_slot

    def replace_sidebar(self, widget: QWidget) -> None:
        """Swap the left pane content, keeping the container layout."""
        self._swap(self._sidebar_slot, widget)

    def replace_list(self, widget: QWidget) -> None:
        """Swap the center pane content."""
        self._swap(self._list_slot, widget)

    def replace_detail(self, widget: QWidget) -> None:
        """Swap the right pane content."""
        self._swap(self._detail_slot, widget)

    def attach_sidebar(self, sidebar: QWidget) -> None:
        """Install the sidebar pane and forward its filter signals.

        Acceptance for P4.2 (信号触发 + 列表联动): the window re-emits
        ``category_selected``/``region_selected`` so the news list and
        search widgets can subscribe to the window without knowing the
        sidebar implementation.
        """
        self.replace_sidebar(sidebar)
        forward = getattr(sidebar, "category_selected", None)
        if forward is not None:
            forward.connect(self.category_selected)
        forward = getattr(sidebar, "region_selected", None)
        if forward is not None:
            forward.connect(self.region_selected)

    def set_status(self, text: str, timeout: int = 0) -> None:
        """Show ``text`` in the status bar (persistent when timeout is 0)."""
        self.statusBar().showMessage(text, timeout)

    # ------------------------------------------------------------------
    # Settings wiring (P4.6)
    # ------------------------------------------------------------------

    def attach_settings_path(self, path: Path) -> None:
        """Point settings persistence at ``path`` (defaults to template)."""
        self._settings_path = Path(path)

    def open_settings(self) -> None:
        """Open the dialog; persist to YAML and announce changes on OK."""
        from core.utils.config import load_settings, save_settings
        from ui.settings_dialog import SettingsDialog

        dialog = SettingsDialog(
            load_settings(self._settings_path), parent=self
        )
        if not dialog.exec():
            return
        edited = dialog.result_settings()
        if edited is None:
            return
        save_settings(edited, self._settings_path)
        self.settings_applied.emit(edited)

    def apply_settings(self, settings: dict) -> None:
        """Consume an applied settings dict (P4.7 instant theme switch)."""
        theme = str(settings.get("ui", {}).get("theme", "light"))
        app = QApplication.instance()
        if app is not None:
            apply_theme(app, theme)

    def show_refresh_report(self, rows: list) -> None:
        """Show/raise the per-source refresh statistics (user request #4)."""
        from ui.refresh_report import RefreshReportDialog

        if self._report_dialog is None:
            self._report_dialog = RefreshReportDialog(self)
        self._report_dialog.load_rows(rows)
        self._report_dialog.show()
        self._report_dialog.raise_()

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    def _build_central(self) -> None:
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self._sidebar_slot = self._make_pane("侧边栏（P4.2）")
        self._list_slot = self._make_pane("新闻列表（P4.3）")
        self._detail_slot = self._make_pane("详情（P4.4）")
        for pane in (self._sidebar_slot, self._list_slot, self._detail_slot):
            splitter.addWidget(pane)
        splitter.setSizes(
            [_SIDEBAR_WIDTH, _LIST_WIDTH, self.width() - _SIDEBAR_WIDTH - _LIST_WIDTH]
        )
        splitter.setStretchFactor(1, 1)
        self.setCentralWidget(splitter)

    def _make_pane(self, text: str) -> QWidget:
        container = QWidget(self)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        placeholder = QLabel(text, container)
        placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(placeholder)
        return container

    def _swap(self, slot: QWidget, widget: QWidget) -> None:
        old = slot.layout().takeAt(0)
        if old is not None:
            old.widget().deleteLater()
        slot.layout().addWidget(widget)

    def _build_actions(self) -> None:
        self.act_refresh = QAction("刷新", self)
        self.act_refresh.setObjectName("act_refresh")
        self.act_refresh.setShortcut(QKeySequence(Qt.Key.Key_F5))
        self.act_refresh.triggered.connect(self.refresh_requested.emit)

        self.act_settings = QAction("设置", self)
        self.act_settings.setObjectName("act_settings")
        self.act_settings.setShortcut(QKeySequence("Ctrl+,"))
        # NOTE: settings_requested -> open_settings is wired in main.py
        # (P4.8) so the signal contract stays side-effect free.
        self.act_settings.triggered.connect(self.settings_requested.emit)

        self.act_quit = QAction("退出", self)
        self.act_quit.setObjectName("act_quit")
        self.act_quit.setShortcut(QKeySequence("Ctrl+Q"))
        self.act_quit.triggered.connect(self.close)

        # Register on the window itself so shortcuts stay live even when
        # the menu bar is hidden, and so tests can look actions up here.
        for action in (self.act_refresh, self.act_settings, self.act_quit):
            self.addAction(action)

    def _build_menus(self) -> None:
        menu_file = self.menuBar().addMenu("文件(&F)")
        menu_file.addAction(self.act_refresh)
        menu_file.addSeparator()
        menu_file.addAction(self.act_settings)
        menu_file.addSeparator()
        menu_file.addAction(self.act_quit)

    def _build_status_bar(self) -> None:
        self.statusBar().showMessage("就绪")
