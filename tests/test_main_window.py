"""P4.1 acceptance tests: MainWindow boots without errors (PRD section 6).

The signal-slot contract defined here is consumed by P4.2-P4.8:
- refresh_requested / settings_requested   -> menu & shortcut triggers
- category_selected / region_selected      -> sidebar filter linkage
- search_submitted                         -> FTS5 filtering
- article_selected                         -> detail pane population
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6", reason="UI extra not installed")

from PySide6.QtGui import QAction  # noqa: E402
from PySide6.QtWidgets import QLabel  # noqa: E402

from ui.main_window import MainWindow  # noqa: E402


def test_startup_without_error(qapp) -> None:
    """Acceptance: the window constructs, shows and closes cleanly."""
    win = MainWindow()
    win.show()
    try:
        assert win.windowTitle() == "GlobalNewsHub"
        assert win.isVisible()
    finally:
        win.close()
    assert not win.isVisible()


def test_layout_slots_exist(qapp) -> None:
    """The three-pane skeleton exposes replaceable slots for P4.2-P4.4."""
    win = MainWindow()
    assert win.sidebar_slot.findChildren(QLabel)
    assert win.list_slot.findChildren(QLabel)
    assert win.detail_slot.findChildren(QLabel)


def test_refresh_shortcut_emits_signal(qapp) -> None:
    win = MainWindow()
    received: list[bool] = []
    win.refresh_requested.connect(lambda: received.append(True))
    action = _find_action(win, "act_refresh")
    assert action is not None
    action.trigger()
    assert received == [True]


def test_settings_action_emits_signal(qapp) -> None:
    win = MainWindow()
    received: list[bool] = []
    win.settings_requested.connect(lambda: received.append(True))
    action = _find_action(win, "act_settings")
    assert action is not None
    action.trigger()
    assert received == [True]


def test_set_status_updates_bar(qapp) -> None:
    win = MainWindow()
    win.set_status("3 篇文章")
    assert "3 篇文章" in win.statusBar().currentMessage()


def _find_action(win: MainWindow, object_name: str) -> QAction | None:
    """Look up an action by its objectName among the window's actions."""
    for action in win.actions():
        if action.objectName() == object_name:
            return action
    return None
