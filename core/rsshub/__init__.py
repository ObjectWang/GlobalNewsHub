"""Embedded RSSHub integration package.

Manages the packaged RSSHub binary lifecycle per the RSSHubManager
interface contract (§4.2).
"""

from core.rsshub.manager import (
    DEFAULT_PORT,
    HEALTHZ_PATH,
    STARTUP_TIMEOUT_SECONDS,
    STOP_TIMEOUT_SECONDS,
    RSSHubManager,
)

__all__ = [
    "DEFAULT_PORT",
    "HEALTHZ_PATH",
    "STARTUP_TIMEOUT_SECONDS",
    "STOP_TIMEOUT_SECONDS",
    "RSSHubManager",
]
