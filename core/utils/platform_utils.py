"""Frozen-aware filesystem anchors (PRD section 10: cross-platform paths).

PyInstaller onedir layout assumed:

- read-only resources (config/, ui/themes, resources/) live under
  ``sys._MEIPASS`` when frozen and under the repository root in dev;
- writable runtime data (SQLite DB, logs) lives next to the executable
  when frozen (``<exe_dir>/data``) and under ``<repo>/data`` in dev.

Every module that touches the filesystem must anchor through these
helpers instead of ``__file__``/cwd arithmetic.
"""

from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    """True when running inside a PyInstaller bundle."""
    return bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    """Read-only resource root (config/, resources/, ui/themes)."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", ".")).resolve()
    return Path(__file__).resolve().parents[2]


def data_dir() -> Path:
    """Writable runtime-data directory; created lazily by callers."""
    if is_frozen():
        base = Path(sys.executable).resolve().parent
    else:
        base = app_root()
    return base / "data"


def default_settings_path() -> Path:
    """Bundled settings template location."""
    return app_root() / "config" / "settings.yaml"


def resources_dir() -> Path:
    """Bundled resources (models, RSSHub binaries, icons)."""
    return app_root() / "resources"
