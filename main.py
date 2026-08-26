"""GlobalNewsHub application entry point (PRD task P4.8).

Assembles the Phase 4 widgets into the MainWindow skeleton and wires
every signal-slot contract. Hard rule (section 1.2): database IO,
network fetching and model inference run ONLY inside the QThread
workers from :mod:`ui.workers` — the UI thread stays responsive.

Run with ``python main.py``.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Final

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

from core.fetcher.scheduler import RefreshScheduler
from core.utils.config import DEFAULT_SETTINGS_PATH, load_settings
from core.utils.logger import setup_logging
from ui.main_window import MainWindow, apply_theme
from ui.news_detail import NewsDetailWidget
from ui.news_list import NewsListWidget
from ui.search_bar import SearchBar
from ui.sidebar import SidebarWidget
from ui.workers import QueryWorker, RefreshWorker

logger = logging.getLogger(__name__)

_DEFAULT_SETTINGS_PATH: Final = DEFAULT_SETTINGS_PATH


class _RefreshBridge(QObject):
    """Timer ticks cross threads via this main-thread-owned signal."""

    refresh_due = Signal()


class AppController(QObject):
    """Owns workers and translates UI signals into background jobs."""

    #: emitted from the APScheduler thread; Qt delivers it queued.
    bridge_refresh_due = Signal()

    def __init__(self, win: MainWindow, news_list: NewsListWidget,
                 detail: NewsDetailWidget, settings_path: Path) -> None:
        super().__init__(win)
        self._win = win
        self._list = news_list
        self._detail = detail
        self._settings_path = settings_path
        self._generation = 0          # stale-reply guard for queries
        self._refresh_worker: RefreshWorker | None = None
        self._query_worker: QueryWorker | None = None
        self._search_text = ""
        self._scheduler: RefreshScheduler | None = None

    # -- wiring ---------------------------------------------------------------

    def wire(self, *, auto_refresh: bool = True) -> None:
        """Connect every contract; call once after widgets are installed."""
        self._win.category_selected.connect(lambda _: self.reload_articles())
        self._win.region_selected.connect(lambda _: self.reload_articles())
        self._win.search_submitted.connect(self.on_search)
        self._win.refresh_requested.connect(self.start_refresh)
        self._win.settings_applied.connect(self._win.apply_settings)
        self._list.article_selected.connect(self.on_article_selected)

        if auto_refresh:
            settings = load_settings(self._settings_path)
            scheduler_cfg = settings.get("scheduler", {})
            if bool(scheduler_cfg.get("auto_refresh_enabled", True)):
                minutes = int(scheduler_cfg.get("refresh_interval_minutes", 30))
                self._scheduler = RefreshScheduler(
                    self.bridge_refresh_due.emit, interval_minutes=minutes
                )
                self._scheduler.start()

    # -- slots ------------------------------------------------------------------

    def reload_articles(self) -> None:
        """Re-query the list honouring sidebar filters + search text."""
        self._generation += 1
        generation = self._generation
        db_path = self._db_path()
        worker = QueryWorker.list_articles(
            db_path,
            category=self._current_filter("category"),
            region=self._current_filter("region"),
        )
        worker.results_ready.connect(
            lambda rows, g=generation: self._on_list_results(g, rows)
        )
        self._replace_query_worker(worker)
        worker.start()

    def on_search(self, text: str) -> None:
        """FTS5 live filtering (P4.5): empty query restores browse mode."""
        self._search_text = text
        if not text:
            self.reload_articles()
            return
        self._generation += 1
        generation = self._generation
        worker = QueryWorker.search(self._db_path(), text)
        worker.results_ready.connect(
            lambda rows, g=generation: self._on_list_results(g, rows)
        )
        self._replace_query_worker(worker)
        worker.start()
        self._win.set_status(f"搜索：{text}")

    def on_article_selected(self, article_id: str) -> None:
        """Load the clicked article into the detail pane off-thread."""
        generation = self._generation
        worker = QueryWorker.detail(self._db_path(), article_id)
        worker.results_ready.connect(
            lambda rows, g=generation: self._on_detail_results(g, rows)
        )
        self._replace_query_worker(worker)
        worker.start()

    def start_refresh(self) -> None:
        """Launch a RefreshWorker unless one is already running."""
        if self._refresh_worker is not None and self._refresh_worker.isRunning():
            self._win.set_status("刷新进行中…")
            return
        settings = load_settings(self._settings_path)
        root = Path(__file__).resolve().parent
        self._refresh_worker = RefreshWorker(
            db_path=self._db_path(),
            sources_path=root / "config" / "sources.yaml",
            models_dir=root / "resources" / "models",
            public_fallback_url=str(
                settings.get("rsshub", {}).get("public_fallback_url",
                                               "https://rsshub.app")),
            max_concurrent_fetches=int(
                settings.get("scheduler", {}).get("max_concurrent_fetches", 5)),
        )
        self._refresh_worker.progress.connect(
            lambda msg: self._win.set_status(f"抓取 {msg}"))
        self._refresh_worker.articles_stored.connect(self._on_stored)
        self._refresh_worker.finished_ok.connect(
            lambda n: self._win.set_status(f"刷新完成，新增 {n} 篇"))
        self._refresh_worker.failed.connect(
            lambda err: self._win.set_status(f"刷新失败：{err}"))
        self._refresh_worker.finished.connect(self._refresh_worker.deleteLater)
        self._refresh_worker.finished.connect(
            lambda: setattr(self, "_refresh_worker", None))
        self._refresh_worker.start()
        self._win.set_status("正在刷新…")

    # -- internals -----------------------------------------------------------

    def _on_list_results(self, generation: int, rows: list) -> None:
        if generation != self._generation:
            return  # stale reply; filters changed meanwhile
        self._list.set_articles(rows)
        self._win.set_status(f"{len(rows)} 篇文章")

    def _on_detail_results(self, generation: int, rows: list) -> None:
        if generation != self._generation:
            return
        self._detail.show_article(rows[0] if rows else None)

    def _on_stored(self, articles: list) -> None:
        if articles:
            current = list(self._list.model()._articles)
            self._list.set_articles(articles + current)

    def _current_filter(self, kind: str) -> str | None:
        sidebars = self._win.sidebar_slot.findChildren(SidebarWidget)
        if not sidebars:
            return None
        sb = sidebars[0]
        return sb.current_category() if kind == "category" else sb.current_region()

    def _db_path(self) -> Path:
        settings = load_settings(self._settings_path)
        raw = str(settings.get("paths", {}).get("database", "data/globalnewshub.db"))
        path = Path(raw)
        return path if path.is_absolute() else Path(__file__).resolve().parent / path

    def _replace_query_worker(self, worker: QueryWorker) -> None:
        old = self._query_worker
        self._query_worker = worker
        if old is not None:
            old.finished.connect(old.deleteLater)


def bootstrap(*, load_initial: bool = True, auto_refresh: bool = False
              ) -> tuple[QApplication, MainWindow]:
    """Build app+window+controller without entering the event loop.

    ``load_initial=False`` keeps tests free of background threads;
    ``auto_refresh`` is enabled only by :func:`main`.
    """
    app = QApplication.instance() or QApplication(sys.argv)
    settings = load_settings(_DEFAULT_SETTINGS_PATH)
    apply_theme(app, str(settings.get("ui", {}).get("theme", "light")))

    win = MainWindow()
    win.attach_settings_path(_DEFAULT_SETTINGS_PATH)

    sidebar = SidebarWidget()
    win.attach_sidebar(sidebar)

    news_list = NewsListWidget()
    search_bar = SearchBar()
    center = QWidget(win)
    layout = QVBoxLayout(center)
    layout.setContentsMargins(6, 6, 6, 0)
    layout.addWidget(search_bar)
    layout.addWidget(news_list, stretch=1)
    win.replace_list(center)

    detail = NewsDetailWidget()
    win.replace_detail(detail)

    controller = AppController(win, news_list, detail, _DEFAULT_SETTINGS_PATH)
    controller.wire(auto_refresh=auto_refresh)

    win.refresh_requested.connect(controller.start_refresh)
    win.search_submitted.connect(controller.on_search)
    news_list.article_selected.connect(controller.on_article_selected)
    controller.bridge_refresh_due.connect(controller.start_refresh)
    win.settings_applied.connect(lambda _: controller.reload_articles())

    if load_initial:
        controller.reload_articles()
    return app, win


def main() -> int:
    """Entry point: bootstrap, show, schedule, execute."""
    settings = load_settings(_DEFAULT_SETTINGS_PATH)
    log_file = Path(__file__).resolve().parent / str(
        settings.get("paths", {}).get("log_file", "data/logs/globalnewshub.log")
    )
    setup_logging(log_file=log_file)
    logger.info("GlobalNewsHub starting")
    app, win = bootstrap(load_initial=True, auto_refresh=True)
    win.show()
    try:
        return app.exec()
    finally:
        logger.info("GlobalNewsHub exited")


if __name__ == "__main__":
    sys.exit(main())
