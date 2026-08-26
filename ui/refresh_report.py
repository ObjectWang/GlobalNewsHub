"""Refresh statistics report dialog (user request #4).

Shows, per source: status (成功 / 失败·疑似被墙), channel used, newly
stored article count and the last error for failures — plus a summary
header. Rendered non-modally after every manual refresh.
"""

from __future__ import annotations

from typing import Any, Final

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

_COLUMNS: Final[tuple[str, ...]] = ("源", "状态", "新增", "通道", "备注")

_CHANNEL_LABELS: Final[dict[str, str]] = {
    "rss": "直连 RSS",
    "local_rsshub": "内嵌 RSSHub",
    "public_rsshub": "公共实例(降级)",
}


def summarize(rows: list[dict[str, Any]]) -> str:
    """One-line summary for the header label."""
    total = len(rows)
    ok = sum(1 for row in rows if row.get("ok"))
    new = sum(int(row.get("new", 0)) for row in rows)
    blocked = [str(row.get("name") or row.get("source_id")) for row in rows if not row.get("ok")]
    head = f"共 {total} 源：成功 {ok} · 失败 {total - ok} · 新增 {new} 篇"
    if blocked:
        head += "；未连通：" + "、".join(blocked[:6]) + ("…" if len(blocked) > 6 else "")
        head += "（境外源需在 settings.yaml 配置 network.proxy）"
    return head


class RefreshReportDialog(QDialog):
    """Non-modal statistics table shown after a refresh finishes."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("刷新统计")
        self.resize(560, 380)
        layout = QVBoxLayout(self)
        self._summary = QLabel(self)
        self._table = QTableWidget(0, len(_COLUMNS), self)
        self._table.setHorizontalHeaderLabels(list(_COLUMNS))
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setColumnWidth(0, 150)
        self._table.setColumnWidth(1, 110)
        self._table.setColumnWidth(2, 60)
        self._table.setColumnWidth(3, 110)
        layout.addWidget(self._summary)
        layout.addWidget(self._table)

    def load_rows(self, rows: list[dict[str, Any]]) -> None:
        """Populate the summary and the per-source table."""
        self._summary.setText(summarize(rows))
        self._table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            name = str(row.get("name") or row.get("source_id") or "")
            ok = bool(row.get("ok"))
            channel = _CHANNEL_LABELS.get(
                str(row.get("channel") or ""), "—" if ok else "不可达"
            )
            error = str(row.get("error") or "")
            cells = (
                name,
                "✅ 成功" if ok else "❌ 失败",
                str(int(row.get("new", 0))),
                channel,
                "" if ok else (error[:80] + ("…" if len(error) > 80 else "")),
            )
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._table.setItem(r, c, item)
