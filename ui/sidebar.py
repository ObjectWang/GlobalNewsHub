"""Sidebar filter panel (PRD task P4.2).

Two single-selection lists — 栏目 (category) and 地区 (region) — each
prefixed by a 全部 row that clears the filter. Selecting a row emits the
corresponding signal with the filter key; empty string means no filter.

Signal-slot contract (consumed by the news list P4.3 / search P4.5 via
``MainWindow.attach_sidebar``):

- ``category_selected(str)``: key from CATEGORIES or ""
- ``region_selected(str)``:   key from REGIONS or ""
"""

from __future__ import annotations

from typing import Final

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QGroupBox,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.constants import CATEGORIES, REGIONS

CATEGORY_LABELS: Final[dict[str, str]] = {
    "politics": "时政",
    "economy": "财经",
    "military": "军事",
    "life": "生活",
    "tech": "科技",
    "other": "其他",
}

REGION_LABELS: Final[dict[str, str]] = {
    "china": "中国",
    "us": "美国",
    "eu": "欧盟",
    "asia": "亚洲",
    "me": "中东",
    "global": "国际",
    "unknown": "未知",
}

_ROLE_KEY = Qt.ItemDataRole.UserRole  # data role storing the filter key


class SidebarWidget(QWidget):
    """Category/region filter panel emitting selection signals."""

    category_selected = Signal(str)
    region_selected = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        self._category_list = self._make_group(
            layout, "栏目", "sidebar_categories", CATEGORY_LABELS
        )
        self._category_list.currentItemChanged.connect(self._on_category_changed)
        self._region_list = self._make_group(
            layout, "地区", "sidebar_regions", REGION_LABELS
        )
        self._region_list.currentItemChanged.connect(self._on_region_changed)
        layout.addStretch(1)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def current_category(self) -> str:
        """Selected category key; "" when 全部 (no filtering)."""
        return self._key_of(self._category_list)

    def current_region(self) -> str:
        """Selected region key; "" when 全部 (no filtering)."""
        return self._key_of(self._region_list)

    def reset(self) -> None:
        """Restore both filters to 全部 without emitting signals."""
        for lst in (self._category_list, self._region_list):
            lst.blockSignals(True)
            lst.setCurrentRow(0)
            lst.blockSignals(False)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _make_group(
        self,
        outer: QVBoxLayout,
        title: str,
        object_name: str,
        labels: dict[str, str],
    ) -> QListWidget:
        group = QGroupBox(title, self)
        inner = QVBoxLayout(group)
        inner.setContentsMargins(4, 4, 4, 4)
        widget = QListWidget(group)
        widget.setObjectName(object_name)
        widget.addItem(self._make_item("全部", ""))
        for key in labels:
            if key in CATEGORIES or key in REGIONS:
                widget.addItem(self._make_item(labels[key], key))
        inner.addWidget(widget)
        outer.addWidget(group)
        return widget

    @staticmethod
    def _make_item(label: str, key: str) -> QListWidgetItem:
        item = QListWidgetItem(label)
        item.setData(_ROLE_KEY, key)
        return item

    @staticmethod
    def _key_of(widget: QListWidget) -> str:
        item = widget.currentItem()
        return str(item.data(_ROLE_KEY)) if item is not None else ""

    def _on_category_changed(
        self, current: QListWidgetItem | None, _previous: QListWidgetItem | None = None
    ) -> None:
        if current is not None:
            self.category_selected.emit(str(current.data(_ROLE_KEY)))

    def _on_region_changed(
        self, current: QListWidgetItem | None, _previous: QListWidgetItem | None = None
    ) -> None:
        if current is not None:
            self.region_selected.emit(str(current.data(_ROLE_KEY)))
