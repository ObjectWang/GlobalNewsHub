"""Application logging setup (console + rotating-ish file sink)."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_DEFAULT_LOG_FILE = Path("data") / "logs" / "globalnewshub.log"

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def setup_logging(
    log_file: Path | None = None,
    level: int = logging.INFO,
    console_level: int | None = None,
) -> None:
    """Configure root logging once; safe to call multiple times.

    ``log_file`` defaults to ``data/logs/globalnewshub.log`` relative to
    the project root. The parent directory is created on demand.
    """
    root = logging.getLogger()
    if getattr(root, "_globalnewshub_configured", False):
        return

    target = Path(log_file) if log_file is not None else _DEFAULT_LOG_FILE
    target.parent.mkdir(parents=True, exist_ok=True)

    root.setLevel(level)
    file_handler = logging.FileHandler(target, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(file_handler)

    console = logging.StreamHandler(stream=sys.stderr)
    console.setLevel(console_level if console_level is not None else level)
    console.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(console)

    root._globalnewshub_configured = True  # type: ignore[attr-defined]
