"""P4.8 acceptance tests: background workers keep IO/inference off UI (§6)."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6", reason="UI extra not installed")

from core.models import Article  # noqa: E402
from core.storage.crud import list_articles  # noqa: E402
from core.storage.database import Database  # noqa: E402
from ui.workers import QueryWorker, RefreshWorker, classify_article  # noqa: E402


def make_article(i: int, title: str = "示例新闻") -> Article:
    return Article(
        id=f"id-{i}",
        title=f"{title}{i}",
        summary=None,
        content=None,
        source_media="测试源",
        source_url=f"https://example.com/{i}",
        category="other",
        region="unknown",
        published_at="2026-08-25T10:00:00",
        fetched_at="2026-08-25T10:00:00",
        tags=[],
    )


def write_sources(tmp_path: Path) -> Path:
    path = tmp_path / "sources.yaml"
    path.write_text(
        "sources:\n"
        "  - id: src_a\n"
        "    name: 测试源\n"
        "    type: rsshub\n"
        "    route: /test/a\n"
        "    category: other\n"
        "    region: unknown\n",
        encoding="utf-8",
    )
    return path


def wait_for(signal_box: list, timeout_s: float = 5.0) -> None:
    """Spin the event loop until the worker signalled or timeout."""
    from PySide6.QtCore import QTimer
    from PySide6.QtTest import QTest

    deadline = QTimer()
    deadline.setSingleShot(True)
    elapsed = 0.0
    step = 0.02
    while not signal_box and elapsed < timeout_s:
        QTest.qWait(int(step * 1000))
        elapsed += step


def test_refresh_worker_stores_and_emits(qapp, tmp_path: Path) -> None:
    sources = write_sources(tmp_path)
    db_path = tmp_path / "t.db"

    async def fake_fetch(url: str, source: object) -> list[Article]:
        return [make_article(1), make_article(2)]

    stored: list[list] = []
    done: list[int] = []
    worker = RefreshWorker(
        db_path=db_path,
        sources_path=sources,
        models_dir=tmp_path / "no_models",
        rsshub_fetch=fake_fetch,
    )
    worker.articles_stored.connect(stored.extend)
    worker.finished_ok.connect(done.append)
    worker.start()
    wait_for(done)
    worker.wait(5000)

    assert done == [2]
    assert len(stored) == 2
    db = Database(db_path)
    db.initialize()
    assert len(list_articles(db)) == 2


def test_refresh_worker_dedups_on_rerun(qapp, tmp_path: Path) -> None:
    """Real-world refetch: SAME source_url, freshly generated uuid."""
    sources = write_sources(tmp_path)
    db_path = tmp_path / "t.db"
    counter = {"n": 0}

    async def fake_fetch(url: str, source: object) -> list[Article]:
        counter["n"] += 1
        art = make_article(100 + counter["n"])  # new uuid each poll
        art.source_url = "https://example.com/stable-7"  # unchanged anchor
        return [art]

    done: list[int] = []
    for _ in range(2):
        round_box: list[int] = []
        worker = RefreshWorker(
            db_path=db_path,
            sources_path=sources,
            models_dir=tmp_path / "no_models",
            rsshub_fetch=fake_fetch,
        )
        worker.finished_ok.connect(round_box.append)
        worker.start()
        wait_for(round_box)
        worker.wait(5000)
        done.extend(round_box)

    assert done == [1, 0]  # second run stores nothing new
    assert len(list_articles(Database(db_path))) == 1


def test_classify_article_keeps_category_domain(qapp) -> None:
    """Keyword/region tiers run without model files; output stays valid."""
    from core.constants import CATEGORIES, REGIONS

    article = make_article(9, title="央行发布货币政策报告")
    classify_article(article)
    assert article.category in CATEGORIES
    assert article.region in REGIONS


def test_query_worker_filters_by_category(qapp, tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    db = Database(db_path)
    db.initialize()
    from core.storage.crud import insert_article

    tech = make_article(1)
    tech.category = "tech"
    pol = make_article(2)
    pol.category = "politics"
    insert_article(db, tech)
    insert_article(db, pol)

    got: list[list] = []
    worker = QueryWorker.list_articles(db_path, category="tech")
    worker.results_ready.connect(got.append)
    worker.start()
    wait_for(got)
    worker.wait(5000)
    assert [a.id for a in got[0]] == ["id-1"]


def test_query_worker_fts_search(qapp, tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    db = Database(db_path)
    db.initialize()
    from core.storage.crud import insert_article

    insert_article(db, make_article(1, title="芯片出口新规"))
    insert_article(db, make_article(2, title="央行降息"))

    got: list[list] = []
    worker = QueryWorker.search(db_path, "芯片")
    worker.results_ready.connect(got.append)
    worker.start()
    wait_for(got)
    worker.wait(5000)
    assert [a.id for a in got[0]] == ["id-1"]


def test_scheduler_run_once_and_lifecycle(qapp) -> None:
    from core.fetcher.scheduler import RefreshScheduler

    fired: list[int] = []

    def callback() -> None:
        fired.append(1)

    sched = RefreshScheduler(callback, interval_minutes=30)
    sched.run_once()
    assert fired == [1]

    sched.start()
    jobs = sched.jobs()
    assert len(jobs) == 1
    sched.stop()


def test_bootstrap_assembles_full_ui(qapp, tmp_path: Path) -> None:
    """Smoke: every Phase 4 widget lands in its window slot, no exec()."""
    import main as app_main
    from core.utils.config import save_settings
    from ui.news_detail import NewsDetailWidget
    from ui.news_list import NewsListWidget
    from ui.search_bar import SearchBar
    from ui.sidebar import SidebarWidget

    settings_file = tmp_path / "settings.yaml"
    save_settings({"paths": {"database": str(tmp_path / "b.db")}}, settings_file)

    app, win = app_main.bootstrap(load_initial=False, settings_path=settings_file)

    try:
        assert win.sidebar_slot.findChildren(SidebarWidget)
        assert win.list_slot.findChildren(SearchBar)
        assert win.list_slot.findChildren(NewsListWidget)
        assert win.detail_slot.findChildren(NewsDetailWidget)
    finally:
        win.close()
