"""App icon artifact + batch title translation worker tests."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6", reason="UI extra not installed")

from PySide6.QtGui import QIcon  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ICO = ROOT / "resources" / "icons" / "globalnewshub.ico"


def test_icon_artifact_exists_and_readable(qapp) -> None:
    assert ICO.is_file() and ICO.stat().st_size > 10_000
    icon = QIcon(str(ICO))
    assert not icon.availableSizes() == []  # Qt parsed the container


def test_main_window_uses_app_icon(qapp) -> None:
    from ui.main_window import MainWindow

    win = MainWindow()
    if not ICO.is_file():
        pytest.skip("icon artifact missing")
    assert win.windowIcon().availableSizes() != []


def test_titles_worker_emits_per_item(qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    from core import translation
    from ui.workers import TranslateTitlesWorker

    async def fake_batch(titles: list[str]) -> list[str]:
        mapping = dict(zip(titles,
                           [f"{t}译" for t in titles], strict=True))
        return [mapping[t] for t in titles]

    monkeypatch.setattr(translation, "translate_titles_batch", fake_batch)

    items = [("a", "Alpha"), ("b", "Beta")]
    worker = TranslateTitlesWorker(items=items)
    got: list[tuple] = []
    done: list[int] = []
    worker.item_translated.connect(lambda aid, zh: got.append((aid, zh)))
    worker.finished_count.connect(done.append)
    worker.start()
    for _ in range(100):
        if done:
            break
        QTest_wait(50)
    worker.wait(5000)
    assert ("a", "Alpha译") in got and ("b", "Beta译") in got
    assert done == [2]


def QTest_wait(ms: int) -> None:
    from PySide6.QtTest import QTest

    QTest.qWait(ms)
