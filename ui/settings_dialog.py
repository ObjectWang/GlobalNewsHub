"""Settings dialog (PRD task P4.6).

Edits a curated subset of ``config/settings.yaml`` sections and hands
the modified tree back to the caller — the dialog itself never touches
the file (persistence lives in :meth:`MainWindow.open_settings`, which
calls :func:`core.utils.config.save_settings` on accept).

Contract:

- constructor takes the current settings dict and never mutates it
- :meth:`result_settings` returns the edited deep copy after ``accept()``
  or ``None`` after ``reject()``
- widget attributes used by tests: ``theme_combo``, ``interval_spin``,
  ``auto_refresh_check``, ``fetch_interval_spin``, ``timeout_spin``,
  ``retry_spin``, ``port_spin``
"""

from __future__ import annotations

import copy
from typing import Any, Final

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QSpinBox,
    QVBoxLayout,
)

_THEMES: Final[tuple[str, ...]] = ("light", "dark")


class SettingsDialog(QDialog):
    """Modal editor over a settings dict; result via :meth:`result_settings`."""

    def __init__(self, settings: dict, parent: Any = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("设置")
        self._original: dict = settings
        self._edited: dict | None = None
        self._build_ui(settings)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def result_settings(self) -> dict | None:
        """Edited settings after accept(); None when rejected/no changes."""
        return self._edited

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self, s: dict) -> None:
        layout = QVBoxLayout(self)

        scheduler = QGroupBox("定时刷新", self)
        form = QFormLayout(scheduler)
        self.auto_refresh_check = QCheckBox("启用自动刷新", scheduler)
        default_enabled = bool(s.get("scheduler", {}).get("auto_refresh_enabled", True))
        self.auto_refresh_check.setChecked(default_enabled)
        form.addRow(self.auto_refresh_check)
        self.interval_spin = QSpinBox(scheduler)
        self.interval_spin.setRange(5, 1440)
        self.interval_spin.setSuffix(" 分钟")
        self.interval_spin.setValue(int(s.get("scheduler", {}).get("refresh_interval_minutes", 30)))
        form.addRow("刷新间隔", self.interval_spin)
        layout.addWidget(scheduler)

        network = QGroupBox("网络与抓取合规", self)
        form = QFormLayout(network)
        self.fetch_interval_spin = QSpinBox(network)
        self.fetch_interval_spin.setRange(1, 60)
        default_interval = int(s.get("network", {}).get("fetch_interval_seconds", 3))
        self.fetch_interval_spin.setValue(default_interval)
        form.addRow("请求最小间隔(秒)", self.fetch_interval_spin)
        self.timeout_spin = QSpinBox(network)
        self.timeout_spin.setRange(1, 60)
        self.timeout_spin.setValue(int(s.get("network", {}).get("request_timeout", 10)))
        form.addRow("请求超时(秒)", self.timeout_spin)
        self.retry_spin = QSpinBox(network)
        self.retry_spin.setRange(0, 10)
        self.retry_spin.setValue(int(s.get("network", {}).get("max_retry", 3)))
        form.addRow("最大重试", self.retry_spin)
        layout.addWidget(network)

        rsshub = QGroupBox("内嵌 RSSHub", self)
        form = QFormLayout(rsshub)
        self.port_spin = QSpinBox(rsshub)
        self.port_spin.setRange(1024, 65535)
        self.port_spin.setValue(int(s.get("rsshub", {}).get("port", 1200)))
        form.addRow("端口（被占用自动后移）", self.port_spin)
        hint = QLabel("公共实例仅在本地服务失败时作为降级兜底。", rsshub)
        hint.setStyleSheet("color: gray;")
        form.addRow(hint)
        layout.addWidget(rsshub)

        appearance = QGroupBox("外观", self)
        form = QFormLayout(appearance)
        self.theme_combo = QComboBox(appearance)
        self.theme_combo.addItems(_THEMES)
        current = str(s.get("ui", {}).get("theme", "light"))
        self.theme_combo.setCurrentText(current if current in _THEMES else "light")
        form.addRow("主题（P4.7 即时生效）", self.theme_combo)
        layout.addWidget(appearance)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # ------------------------------------------------------------------
    # Qt overrides
    # ------------------------------------------------------------------

    def accept(self) -> None:
        """Snapshot edited widgets into ``_edited`` and close as OK."""
        original = self._original
        edited = copy.deepcopy(original)

        def section(name: str) -> dict:
            return edited.setdefault(name, {}) if isinstance(edited.get(name, {}), dict) else {}

        section("scheduler")["auto_refresh_enabled"] = self.auto_refresh_check.isChecked()
        section("scheduler")["refresh_interval_minutes"] = int(self.interval_spin.value())
        section("network")["fetch_interval_seconds"] = int(self.fetch_interval_spin.value())
        section("network")["request_timeout"] = int(self.timeout_spin.value())
        section("network")["max_retry"] = int(self.retry_spin.value())
        section("rsshub")["port"] = int(self.port_spin.value())
        section("ui")["theme"] = self.theme_combo.currentText()

        self._edited = edited
        super().accept()

    def reject(self) -> None:
        """Discard edits; ``result_settings`` stays None."""
        self._edited = None
        super().reject()
