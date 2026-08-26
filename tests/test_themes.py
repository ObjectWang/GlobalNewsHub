"""P4.7 acceptance tests: QSS themes switch instantly (§6)."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6", reason="UI extra not installed")

from PySide6.QtWidgets import QApplication  # noqa: E402

import ui.main_window as mw  # noqa: E402
from ui.main_window import MainWindow, apply_theme  # noqa: E402


@pytest.fixture()
def clean_stylesheet(qapp) -> None:
    """Ensure a neutral starting sheet and restore afterwards."""
    qapp.setStyleSheet("")
    yield
    qapp.setStyleSheet("")


def test_light_and_dark_sheets_exist_and_differ(clean_stylesheet) -> None:
    light = mw.load_theme_sheet("light")
    dark = mw.load_theme_sheet("dark")
    assert light.strip() != ""
    assert dark.strip() != ""
    assert light != dark


def test_apply_theme_sets_application_sheet(qapp, clean_stylesheet) -> None:
    apply_theme(qapp, "dark")
    assert qapp.styleSheet() == mw.load_theme_sheet("dark")
    apply_theme(qapp, "light")
    assert qapp.styleSheet() == mw.load_theme_sheet("light")


def test_unknown_theme_falls_back_to_light(qapp, clean_stylesheet) -> None:
    apply_theme(qapp, "solarized")
    assert qapp.styleSheet() == mw.load_theme_sheet("light")


def test_main_window_applies_theme_from_settings(
    qapp, clean_stylesheet
) -> None:
    """联动 contract: settings_applied carries ui.theme -> instant restyle."""
    win = MainWindow()
    received: list[dict] = []
    win.settings_applied.connect(received.append)

    win.apply_settings({"ui": {"theme": "dark"}})
    assert qapp.styleSheet() == mw.load_theme_sheet("dark")
    assert received == []  # apply_settings consumes, not emits

    win.apply_settings({"ui": {"theme": "light"}})
    assert qapp.styleSheet() == mw.load_theme_sheet("light")


def test_open_settings_flow_updates_theme_instantly(
    qapp, tmp_path, monkeypatch, clean_stylesheet
) -> None:
    """End-to-end P4.6+P4.7: dialog OK -> YAML saved -> restyle applied."""
    from core.utils.config import load_settings, save_settings
    from ui.settings_dialog import SettingsDialog

    target = tmp_path / "settings.yaml"
    save_settings({"ui": {"theme": "light"}}, target)

    win = MainWindow()
    win.attach_settings_path(target)

    def fake_exec(self: SettingsDialog) -> int:
        self.theme_combo.setCurrentText("dark")
        self.accept()
        return 1

    monkeypatch.setattr(SettingsDialog, "exec", fake_exec)
    # main.py wires this in production; replicate here for the flow test.
    win.settings_requested.connect(win.open_settings)
    win.settings_applied.connect(win.apply_settings)
    win.act_settings.trigger()

    assert load_settings(target)["ui"]["theme"] == "dark"
    assert QApplication.instance() is not None
    app = QApplication.instance()
    assert app is not None
    assert app.styleSheet() == mw.load_theme_sheet("dark")
