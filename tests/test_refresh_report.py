"""User-request #4 tests: refresh report rows + statistics dialog."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6", reason="UI extra not installed")

from ui.refresh_report import RefreshReportDialog, summarize  # noqa: E402

SAMPLE_ROWS = [
    {"source_id": "people", "name": "人民网", "ok": True,
     "channel": "rss", "new": 12, "error": ""},
    {"source_id": "zhihu", "name": "知乎热榜", "ok": False,
     "channel": None, "new": 0,
     "error": "GET https://rsshub.app/zhihu/hotlist failed after 4 attempts"},
    {"source_id": "36kr", "name": "36氪快讯", "ok": True,
     "channel": "local_rsshub", "new": 20, "error": ""},
]


def test_summarize_counts_and_blocked_names() -> None:
    text = summarize(SAMPLE_ROWS)
    assert "共 3 源" in text
    assert "成功 2" in text
    assert "失败 1" in text
    assert "新增 32 篇" in text
    assert "知乎热榜" in text  # blocked source surfaced by name


def test_dialog_populates_table(qapp) -> None:
    dlg = RefreshReportDialog()
    dlg.load_rows(SAMPLE_ROWS)
    assert dlg._table.rowCount() == 3
    assert dlg._table.item(0, 0).text() == "人民网"
    assert "✅" in dlg._table.item(0, 1).text()
    assert dlg._table.item(1, 1).text().startswith("❌")
    assert dlg._table.item(2, 3).text() == "内嵌 RSSHub"
    assert "rsshub.app" in dlg._table.item(1, 4).text()


def test_worker_emits_refresh_report(qapp, tmp_path) -> None:
    """One OK + one failing source produce exactly two report rows."""

    sources = tmp_path / "sources.yaml"
    sources.write_text(
        "sources:\n"
        "  - id: good\n"
        "    name: 好源\n"
        "    type: rsshub\n"
        "    route: /good\n"
        "  - id: bad\n"
        "    name: 坏源\n"
        "    type: rss\n"
        "    url: http://bad.example/rss\n",
        encoding="utf-8",
    )
    from ui.workers import RefreshWorker

    async def fake_rsshub(url: str, source: object):
        return []

    async def fake_rss(source):
        raise RuntimeError("connection timed out")

    reports: list[list] = []
    done: list[int] = []
    worker = RefreshWorker(
        db_path=tmp_path / "t.db",
        sources_path=sources,
        models_dir=tmp_path / "nomodels",
        rss_fetch=fake_rss,
        rsshub_fetch=fake_rsshub,
    )
    worker.refresh_report.connect(reports.append)
    worker.finished_ok.connect(done.append)
    worker.start()
    for _ in range(200):
        if done:
            break
        from PySide6.QtTest import QTest

        QTest.qWait(100)
    worker.wait(5000)

    assert len(reports) == 1
    rows = {row["source_id"]: row for row in reports[0]}
    assert rows["good"]["ok"] is True and rows["good"]["name"] == "好源"
    assert rows["bad"]["ok"] is False and "timed out" in rows["bad"]["error"]
