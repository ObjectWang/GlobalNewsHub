"""P5 path-anchor tests: frozen bundles must resolve read-only vs writable roots."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_dev_mode_anchors_at_repo_root() -> None:
    from core.utils.platform_utils import app_root, data_dir

    root = app_root()
    assert (root / "config" / "settings.yaml").is_file()
    assert (root / "main.py").is_file()
    assert data_dir() == root / "data"


def test_frozen_mode_splits_readonly_and_writable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Simulate PyInstaller: sys.frozen + _MEIPASS + exe outside bundle."""
    from core.utils import platform_utils as pu

    meipass = tmp_path / "bundle"
    meipass.mkdir()
    exe_dir = tmp_path / "app"
    exe_dir.mkdir()
    monkeypatch.setattr(pu.sys, "frozen", True, raising=False)
    monkeypatch.setattr(pu.sys, "_MEIPASS", str(meipass), raising=False)
    monkeypatch.setattr(pu.sys, "executable", str(exe_dir / "GlobalNewsHub.exe"))

    assert pu.is_frozen() is True
    assert pu.app_root() == meipass
    assert pu.data_dir() == exe_dir / "data"
    assert pu.default_settings_path() == meipass / "config" / "settings.yaml"
    assert pu.resources_dir() == meipass / "resources"


def test_rsshub_server_log_lands_in_writable_dir() -> None:
    """The embedded server log must target data_dir(), never _MEIPASS."""
    from core.rsshub.manager import log_path
    from core.utils.platform_utils import data_dir

    assert log_path() == data_dir() / "logs" / "rsshub-server.log"
