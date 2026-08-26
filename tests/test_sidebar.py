"""P4.2 acceptance tests: sidebar filters (PRD section 6).

Contract consumed by the news list (P4.3) and search (P4.5):
- ``category_selected(str)`` / ``region_selected(str)`` carry the filter
  key; empty string means "no filter" (the 全部 row).
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6", reason="UI extra not installed")

from PySide6.QtWidgets import QListWidget  # noqa: E402

from ui.sidebar import CATEGORY_LABELS, REGION_LABELS, SidebarWidget  # noqa: E402


def test_default_selection_is_no_filter(qapp) -> None:
    sb = SidebarWidget()
    assert sb.current_category() == ""
    assert sb.current_region() == ""


def test_category_signal_emits_key(qapp) -> None:
    sb = SidebarWidget()
    received: list[str] = []
    sb.category_selected.connect(received.append)
    cat_list = _list(sb, 0)
    cat_list.setCurrentRow(_row_of(cat_list, "economy"))
    assert received == ["economy"]
    assert sb.current_category() == "economy"


def test_region_signal_emits_key(qapp) -> None:
    sb = SidebarWidget()
    received: list[str] = []
    sb.region_selected.connect(received.append)
    region_list = _list(sb, 1)
    region_list.setCurrentRow(_row_of(region_list, "china"))
    assert received == ["china"]
    assert sb.current_region() == "china"


def test_all_rows_emit_empty_string(qapp) -> None:
    """Selecting 全部 after a filter resets it to '' (no filtering)."""
    sb = SidebarWidget()
    received: list[str] = []
    sb.category_selected.connect(received.append)
    cat_list = _list(sb, 0)
    cat_list.setCurrentRow(_row_of(cat_list, "tech"))
    cat_list.setCurrentRow(0)
    assert received == ["tech", ""]
    assert sb.current_category() == ""


def test_reset_restores_defaults(qapp) -> None:
    sb = SidebarWidget()
    _list(sb, 0).setCurrentRow(_row_of(_list(sb, 0), "military"))
    _list(sb, 1).setCurrentRow(_row_of(_list(sb, 1), "eu"))
    sb.reset()
    assert sb.current_category() == ""
    assert sb.current_region() == ""


def test_labels_cover_constants(qapp) -> None:
    """Every PRD category/region key has a display label (§2.2)."""
    from core.constants import CATEGORIES, REGIONS

    assert set(CATEGORY_LABELS) >= set(CATEGORIES)
    assert set(REGION_LABELS) >= set(REGIONS)


def test_main_window_forwards_sidebar_signals(qapp) -> None:
    """联动 contract: MainWindow.attach_sidebar re-emits filter changes."""
    from ui.main_window import MainWindow

    win = MainWindow()
    sb = SidebarWidget()
    win.attach_sidebar(sb)

    categories: list[str] = []
    regions: list[str] = []
    win.category_selected.connect(categories.append)
    win.region_selected.connect(regions.append)

    cat_list = _list(win.sidebar_slot.findChildren(SidebarWidget)[0], 0)
    cat_list.setCurrentRow(_row_of(cat_list, "life"))
    region_list = _list(sb, 1)
    region_list.setCurrentRow(_row_of(region_list, "asia"))

    assert categories == ["life"]
    assert regions == ["asia"]


def _list(sb: SidebarWidget, index: int) -> QListWidget:
    lists = sb.findChildren(QListWidget)
    assert len(lists) == 2
    return sorted(lists, key=lambda w: w.objectName())[index]


def _row_of(lst: QListWidget, key: str) -> int:
    for row in range(lst.count()):
        if lst.item(row).data(0x0100) == key:  # Qt.ItemDataRole.UserRole
            return row
    raise AssertionError(f"key {key!r} not in list")
