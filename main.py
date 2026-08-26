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

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QPushButton, QVBoxLayout, QWidget

from core.fetcher.scheduler import RefreshScheduler
from core.utils import platform_utils
from core.utils.config import load_settings
from core.utils.logger import setup_logging
from core.utils.platform_utils import (
    data_dir,
    default_settings_path,
    resources_dir,
)
from ui.main_window import MainWindow, apply_theme
from ui.news_detail import NewsDetailWidget
from ui.news_list import NewsListWidget
from ui.search_bar import SearchBar
from ui.sidebar import SidebarWidget
from ui.workers import (
    ImageWorker,
    QueryWorker,
    RefreshWorker,
    TranslateTitlesWorker,
    TranslateWorker,
)

logger = logging.getLogger(__name__)


def _detail_of(win):
    """Return the installed NewsDetailWidget for ``win``."""
    from ui.news_detail import NewsDetailWidget
    widgets = win.detail_slot.findChildren(NewsDetailWidget)
    return widgets[0] if widgets else None


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
        self._translate_worker = None
        self._titles_worker = None
        self._translate_all_btn = None
        self._image_workers: list = []
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

    def _spawn(self, worker) -> None:
        """Track and launch a worker (thread teardown is join-based)."""
        self._query_worker = worker
        worker.start()

    def shutdown(self) -> None:
        """Stop background activity and join threads BEFORE Qt teardown.

        Called from ``QApplication.aboutToQuit``: letting interpreter
        finalization destroy live QThread wrappers triggers fastfail
        crashes on Windows (observed P5.1), so we wait for them here.
        """
        logger.info("shutdown: stopping scheduler and workers")
        if self._scheduler is not None:
            self._scheduler.stop()
            self._scheduler = None
        tracked: list = []
        for attr in ("_refresh_worker", "_query_worker", "_translate_worker"):
            worker = getattr(self, attr, None)
            if worker is not None:
                tracked.append(worker)
        tracked.extend(getattr(self, "_image_workers", []) or [])
        for worker in tracked:
            try:
                if worker.is_running():
                    worker.wait(5000)
            except RuntimeError:
                pass  # C++ side already destroyed

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
        self._spawn(worker)

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
        self._spawn(worker)
        self._win.set_status(f"搜索：{text}")

    def on_article_selected(self, article_id: str) -> None:
        """Load the clicked article into the detail pane off-thread."""
        generation = self._generation
        worker = QueryWorker.detail(self._db_path(), article_id)
        worker.results_ready.connect(
            lambda rows, g=generation: self._on_detail_results(g, rows)
        )
        self._spawn(worker)

    def on_images_requested(self, article_id: str, urls: list) -> None:
        """Download remote article images into local cache (request #4)."""
        if not urls:
            return
        settings = load_settings(self._settings_path)
        proxy = str(settings.get("network", {}).get("proxy") or "") or None
        worker = ImageWorker(article_id=article_id, urls=urls, proxy=proxy)
        worker.image_ready.connect(
            lambda aid, url, path: _detail_of(self._win).apply_local_image(
                aid, url, path
            )
        )
        self._image_workers.append(worker)
        worker.start()

    def on_translate_requested(self, article_id: str, text: str) -> None:
        """Translate a non-Chinese article to Chinese (request #3)."""
        settings = load_settings(self._settings_path)
        proxy = str(settings.get("network", {}).get("proxy") or "") or None
        article = self._find_article(article_id)
        title = article.title if article else text
        worker = TranslateWorker(
            article_id=article_id,
            title=title,
            body=text,
            source_lang="en",  # gtx auto-detects; mymemory needs a pair
            proxy=proxy,
        )
        detail = _detail_of(self._win)
        worker.translated.connect(detail.apply_translation)
        worker.failed_sig.connect(
            lambda _aid, err: (detail.set_translating(False),
                               self._win.set_status(f"翻译失败：{err}")))
        self._translate_worker = worker
        worker.start()

    def register_button(self, button) -> None:
        """Keep a reference to the 翻译全部标题 button (enable/disable)."""
        self._translate_all_btn = button

    def translate_all_titles(self) -> None:
        """Batch-translate visible foreign titles (request: 一键翻译所有标题)."""
        if getattr(self, "_titles_worker", None) is not None and \
                self._titles_worker.is_running():
            self._win.set_status("标题翻译进行中…")
            return
        articles = list(self._list.model()._articles)
        items = [
            (a.id, a.title)
            for a in articles
            if not a.language.lower().startswith("zh")
            and a.id not in self._list.model()._translations
        ]
        if not items:
            self._win.set_status("没有需要翻译的外文标题")
            return
        settings = load_settings(self._settings_path)
        proxy = str(settings.get("network", {}).get("proxy") or "") or None
        btn = getattr(self, "_translate_all_btn", None)
        if btn is not None:
            btn.setEnabled(False)
        worker = TranslateTitlesWorker(items=items, proxy=proxy)
        done = {"n": 0}

        def on_item_apply(aid: str, zh: str) -> None:
            self._list.model().set_translation(aid, zh)
            done["n"] += 1
            self._win.set_status(f"标题翻译中… {done['n']}/{len(items)}")

        worker.item_translated.connect(on_item_apply)
        btn_ref = getattr(self, "_translate_all_btn", None)
        worker.finished_count.connect(
            lambda c: (
                self._win.set_status(f"标题翻译完成：{c} 条"),
                None if btn_ref is None else btn_ref.setEnabled(True),
            )
        )
        self._titles_worker = worker
        worker.start()

    def _find_article(self, article_id: str):
        for article in self._list.model()._articles:
            if article.id == article_id:
                return article
        return None

    def start_refresh(self) -> None:
        """Launch a RefreshWorker unless one is already running."""
        if self._refresh_worker is not None and self._refresh_worker.is_running():
            self._win.set_status("刷新进行中…")
            return
        settings = load_settings(self._settings_path)
        network_cfg = settings.get("network", {})
        self._refresh_worker = RefreshWorker(
            db_path=self._db_path(),
            sources_path=default_settings_path().parent / "sources.yaml",
            models_dir=resources_dir() / "models",
            public_fallback_url=str(
                settings.get("rsshub", {}).get("public_fallback_url",
                                               "https://rsshub.app")),
            max_concurrent_fetches=int(
                settings.get("scheduler", {}).get("max_concurrent_fetches", 5)),
            proxy=str(network_cfg.get("proxy") or "") or None,
            rsshub_port=int(settings.get("rsshub", {}).get("port", 1200)),
            autostart_rsshub=True,
        )
        self._refresh_worker.progress.connect(
            lambda msg: self._win.set_status(f"抓取 {msg}"))
        self._refresh_worker.articles_stored.connect(self._on_stored)
        self._refresh_worker.refresh_report.connect(
            lambda rows, n=None: self._win.show_refresh_report(rows))
        self._refresh_worker.finished_ok.connect(
            lambda n: self._win.set_status(f"刷新完成，新增 {n} 篇"))
        self._refresh_worker.failed.connect(
            lambda err: self._win.set_status(f"刷新失败：{err}"))
        self._refresh_worker.start()
        self._win.set_status("正在刷新…")

    # -- internals -----------------------------------------------------------

    def _spawn(self, worker) -> None:
        """Track and launch a worker (thread teardown is join-based)."""
        self._query_worker = worker
        worker.start()

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
        # Relative settings paths anchor at the app root's parent so that
        # dev ("data/...") and frozen (<exe_dir>/data/...) both resolve
        # into the writable tree.
        return path if path.is_absolute() else data_dir().parent / path

def bootstrap(*, load_initial: bool = True, auto_refresh: bool = False,
              settings_path: Path | None = None
              ) -> tuple[QApplication, MainWindow]:
    """Build app+window+controller without entering the event loop.

    ``load_initial=False`` keeps tests free of background threads;
    ``auto_refresh`` is enabled only by :func:`main`; ``settings_path``
    is injectable for tests (defaults to the frozen-aware template).
    """
    sp = Path(settings_path) if settings_path else default_settings_path()
    app = QApplication.instance() or QApplication(sys.argv)
    settings = load_settings(sp)
    apply_theme(app, str(settings.get("ui", {}).get("theme", "light")))

    win = MainWindow()
    win.attach_settings_path(sp)

    sidebar = SidebarWidget()
    win.attach_sidebar(sidebar)

    news_list = NewsListWidget()
    search_bar = SearchBar()

    translate_all_btn = QPushButton("翻译全部标题")
    translate_all_btn.setToolTip("将当前列表中的外文标题批量译为中文")

    from PySide6.QtWidgets import QHBoxLayout

    top_row = QWidget(win)
    top_layout = QHBoxLayout(top_row)
    top_layout.setContentsMargins(0, 0, 0, 0)
    top_layout.addWidget(search_bar, stretch=1)
    top_layout.addWidget(translate_all_btn)

    center = QWidget(win)
    layout = QVBoxLayout(center)
    layout.setContentsMargins(6, 6, 6, 0)
    layout.addWidget(top_row)
    layout.addWidget(news_list, stretch=1)
    win.replace_list(center)

    detail = NewsDetailWidget()
    win.replace_detail(detail)

    controller = AppController(win, news_list, detail, sp)
    controller.wire(auto_refresh=auto_refresh)
    app.aboutToQuit.connect(controller.shutdown)

    win.refresh_requested.connect(controller.start_refresh)
    win.search_submitted.connect(controller.on_search)
    news_list.article_selected.connect(controller.on_article_selected)
    controller.bridge_refresh_due.connect(controller.start_refresh)
    win.settings_applied.connect(lambda _: controller.reload_articles())

    detail = _detail_of(win)
    if detail is not None:
        detail.images_requested.connect(controller.on_images_requested)
        detail.translate_requested.connect(controller.on_translate_requested)

    translate_all_btn.clicked.connect(controller.translate_all_titles)
    translate_all_btn.clicked.connect(
        lambda: controller.register_button(translate_all_btn))

    if load_initial:
        controller.reload_articles()
    return app, win


def main() -> int:
    """Entry point: bootstrap, show, schedule, execute.

    ``--smoke``: auto-quit shortly after showing (packaged-build check).
    """
    settings = load_settings(default_settings_path())
    raw_log = str(settings.get("paths", {}).get("log_file",
                                                "data/logs/globalnewshub.log"))
    log_file = Path(raw_log)
    setup_logging(
        log_file=log_file if log_file.is_absolute() else data_dir().parent / log_file
    )
    logger.info("GlobalNewsHub starting (frozen=%s)", platform_utils.is_frozen())
    app, win = bootstrap(load_initial=True, auto_refresh=True)
    win.show()
    if "--smoke" in sys.argv:
        from PySide6.QtCore import QTimer

        QTimer.singleShot(4000, app.quit)
        logger.info("smoke mode: quitting in 4s")
    try:
        return app.exec()
    finally:
        logger.info("GlobalNewsHub exited")


if __name__ == "__main__":
    sys.exit(main())
