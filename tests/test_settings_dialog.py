"""P4.6 acceptance tests: settings dialog persists to YAML (§6)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytest.importorskip("PySide6", reason="UI extra not installed")

from ui.settings_dialog import SettingsDialog  # noqa: E402


@pytest.fixture()
def settings() -> dict:
    """A representative in-memory settings tree (mirrors the template)."""
    return {
        "scheduler": {"auto_refresh_enabled": True, "refresh_interval_minutes": 30},
        "network": {"fetch_interval_seconds": 3, "request_timeout": 10, "max_retry": 3},
        "rsshub": {"port": 1200},
        "ui": {"theme": "light"},
    }


def test_load_template_settings() -> None:
    """The shipped config/settings.yaml parses and has expected sections."""
    from core.utils.config import load_settings

    loaded = load_settings(Path("config") / "settings.yaml")
    for section in ("app", "paths", "network", "scheduler", "rsshub", "ui"):
        assert section in loaded
    assert loaded["ui"]["theme"] == "light"


def test_save_roundtrip_keeps_values_and_unicode(tmp_path: Path) -> None:
    from core.utils.config import save_settings

    target = tmp_path / "settings.yaml"
    data = {"ui": {"theme": "dark"}, "note": "中文注释保持可读"}
    save_settings(data, target)
    raw = target.read_text(encoding="utf-8")
    assert "中文注释保持可读" in raw  # allow_unicode, no \\u escapes
    assert yaml.safe_load(raw) == data


def test_edits_appear_in_result_after_accept(qapp, settings: dict) -> None:
    dlg = SettingsDialog(settings)
    dlg.theme_combo.setCurrentText("dark")
    dlg.interval_spin.setValue(45)
    _ = dlg.port_spin  # attribute exists (contract); value untouched below
    dlg.accept()
    result = dlg.result_settings()
    assert result["ui"]["theme"] == "dark"
    assert result["scheduler"]["refresh_interval_minutes"] == 45


def test_original_dict_not_mutated_before_accept(qapp, settings: dict) -> None:
    dlg = SettingsDialog(settings)
    dlg.theme_combo.setCurrentText("dark")
    dlg.interval_spin.setValue(90)
    assert settings["ui"]["theme"] == "light"  # untouched
    assert settings["scheduler"]["refresh_interval_minutes"] == 30


def test_cancel_discards_changes(qapp, settings: dict) -> None:
    dlg = SettingsDialog(settings)
    dlg.theme_combo.setCurrentText("dark")
    dlg.reject()
    assert dlg.result_settings() is None  # nothing to persist


def test_main_window_persists_on_accept(
    qapp, settings: dict, tmp_path: Path, monkeypatch
) -> None:
    """联动: act_settings -> dialog -> YAML file updated on OK."""
    from core.utils.config import load_settings
    from ui.main_window import MainWindow

    target = tmp_path / "settings.yaml"
    from core.utils.config import save_settings

    save_settings(settings, target)

    win = MainWindow()
    win.attach_settings_path(target)

    def fake_exec(self) -> int:
        self.theme_combo.setCurrentText("dark")
        self.accept()
        return 1

    monkeypatch.setattr(SettingsDialog, "exec", fake_exec)
    win.open_settings()

    assert load_settings(target)["ui"]["theme"] == "dark"
