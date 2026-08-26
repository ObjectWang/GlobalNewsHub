"""UI layer package.

Runs :func:`ensure_qt_runtime` on import so that every ``ui.*`` module
(and any test importing them) can import PySide6 safely.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)

_QT_DLLS: Final[tuple[str, ...]] = ("Qt6Core.dll", "Qt6Gui.dll", "Qt6Widgets.dll")

_LOAD_WITH_ALTERED_SEARCH_PATH: Final = 0x00000008

_BOOTSTRAPPED: bool = False


def ensure_qt_runtime() -> None:
    """Guarantee that ``PySide6.QtCore`` is importable on this process.

    Some PySide6 wheels fail to load on Windows when the default DLL
    search order resolves Qt's runtime dependencies to incompatible
    system copies (observed with PySide6 6.11 on the reference machine;
    6.8.x imports cleanly). If the plain import fails *gracefully*, the
    bundled ``Qt6*.dll`` set is pinned first via
    ``LOAD_WITH_ALTERED_SEARCH_PATH`` so subsequent imports reuse healthy
    in-process copies.

    Deliberately does NOT preload the bundled VC runtime: python.exe has
    already loaded system copies and two live CRTs corrupt heaps.

    Harmless no-op on other platforms, healthy environments, and when
    applied more than once.
    """
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED or sys.platform != "win32":
        _BOOTSTRAPPED = True
        return
    try:
        import PySide6.QtCore  # noqa: F401  (probe: already importable?)
    except ImportError:
        try:
            import shiboken6  # importable whenever PySide6 is installed

            pyside_dir = (
                Path(shiboken6.__file__).resolve().parent.parent / "PySide6"
            )
            os.add_dll_directory(str(pyside_dir))
            for dll in _QT_DLLS:
                path = pyside_dir / dll
                if path.exists():
                    try:
                        ctypes.WinDLL(
                            str(path), winmode=_LOAD_WITH_ALTERED_SEARCH_PATH
                        )
                    except OSError:
                        logger.warning("Failed to preload %s", path, exc_info=True)
        except Exception:
            logger.exception("Qt runtime bootstrap failed")
            raise
    _BOOTSTRAPPED = True


ensure_qt_runtime()
