"""YAML settings persistence (PRD task P4.6).

``config/settings.yaml`` is the single user-editable override file;
:func:`load_settings` reads it into a plain dict and
:func:`save_settings` writes it back with stable key order and readable
CJK characters. Only sections present are touched; callers merge.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

DEFAULT_SETTINGS_PATH: Path = Path("config") / "settings.yaml"


def load_settings(path: Path | None = None) -> dict[str, Any]:
    """Parse the YAML settings file; missing/empty file yields ``{}``."""
    target = Path(path) if path is not None else DEFAULT_SETTINGS_PATH
    if not target.exists():
        return {}
    with target.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return data if isinstance(data, dict) else {}


def save_settings(settings: dict[str, Any], path: Path | None = None) -> None:
    """Atomically write ``settings`` as human-friendly YAML.

    - original mapping order preserved (``sort_keys=False``)
    - CJK kept literal (``allow_unicode=True``)
    - written via temp file + replace to avoid truncated files on crash
    """
    target = Path(path) if path is not None else DEFAULT_SETTINGS_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(
            settings, fh, sort_keys=False, allow_unicode=True, default_flow_style=False
        )
    tmp.replace(target)
