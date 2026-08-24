"""RSSHub embedded-server lifecycle manager (PRD task P2.2).

Implements the section 4.2 interface contract for the pkg-packaged
RSSHub binary (plan B, section 4.1): start it as a subprocess, block
until /healthz is ready (max startup_timeout_seconds), stop it
gracefully and force-kill after stop_timeout_seconds.

Runtime notes:

- NODE_ENV=production is mandatory: the packaged server loads the
  static route manifest only in production mode; the dev registry scans
  the source tree, which does not exist inside a pkg snapshot.
- Server stdout/stderr goes to data/logs/rsshub-server.log so a hung or
  crashing binary stays diagnosable without blocking on pipe buffers.
- When the configured port is occupied the manager auto-increments
  (section 4.2 _find_available_port; risk matrix "RSSHub port conflict").
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import IO

logger = logging.getLogger(__name__)

DEFAULT_PORT: int = 1200                    # settings.yaml rsshub.port
STARTUP_TIMEOUT_SECONDS: float = 15.0       # section 4.2 start()
STOP_TIMEOUT_SECONDS: float = 5.0           # section 4.2 stop()
HEALTHZ_PATH: str = "/healthz"

_HEALTH_POLL_INTERVAL_SECONDS = 0.3
_HEALTH_REQUEST_TIMEOUT_SECONDS = 1.0
_MAX_PORT_ATTEMPTS = 100

# Resource layout per section 5. PyInstaller builds (P5.1) can override
# via the binary_path constructor argument.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_RESOURCES_DIR = _PROJECT_ROOT / "resources"
_LOG_PATH = _PROJECT_ROOT / "data" / "logs" / "rsshub-server.log"

_BINARY_NAME_BY_PLATFORM: dict[str, str] = {
    "win32": "rsshub-server.exe",
    "darwin": "rsshub-server-mac",
    "linux": "rsshub-server-linux",
}


def _port_is_free(port: int) -> bool:
    """True when a TCP listener can bind 127.0.0.1:port right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _open_log() -> IO[bytes]:
    """Open (creating parents) the RSSHub server log in append mode."""
    _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    return _LOG_PATH.open("ab")


class RSSHubManager:
    """Lifecycle owner of the embedded RSSHub server (section 4.2).

    Synchronous by contract: start() blocks up to 15 s, so callers must
    run it off the UI thread (section 1.2; the Phase 4 integration does
    this in a QThread, P4.8).
    """

    def __init__(
        self,
        port: int = DEFAULT_PORT,
        *,
        binary_path: Path | None = None,
        startup_timeout_seconds: float = STARTUP_TIMEOUT_SECONDS,
        stop_timeout_seconds: float = STOP_TIMEOUT_SECONDS,
        popen_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Store configuration; nothing is spawned until start().

        binary_path, popen_factory, sleep and monotonic are injectable
        for tests (house convention, see core.utils.network.HttpClient).
        """
        self._configured_port = port
        self._port = port
        self._binary_path = binary_path
        self._startup_timeout = startup_timeout_seconds
        self._stop_timeout = stop_timeout_seconds
        self._popen_factory = popen_factory
        self._sleep = sleep
        self._monotonic = monotonic
        self._process: subprocess.Popen[bytes] | None = None
        self._log_handle: IO[bytes] | None = None

    # -- properties ---------------------------------------------------------

    @property
    def port(self) -> int:
        """TCP port the embedded server listens on (valid after start)."""
        return self._port

    @property
    def base_url(self) -> str:
        """Base URL of the embedded server, e.g. http://localhost:1200."""
        return f"http://localhost:{self._port}"

    # -- public contract (section 4.2) ---------------------------------------

    def start(self) -> bool:
        """Spawn the binary and block until /healthz is ready.

        Waits at most startup_timeout_seconds (15 s per settings.yaml).
        Returns False (never raises) when the binary is missing, the
        process exits early, or healthz never becomes ready; in the last
        two cases the half-started process is stopped before returning.
        """
        if self._process is not None:
            if self.is_alive():
                logger.info("RSSHub already running at %s", self.base_url)
                return True
            logger.warning("Stale RSSHub process found; cleaning it up")
            self.stop()
        binary = self._get_binary_path()
        if not binary.is_file():
            logger.error(
                "RSSHub binary missing at %s; run scripts/build_rsshub.sh (P2.1)", binary
            )
            return False
        self._port = self._find_available_port()
        env = {**os.environ, "PORT": str(self._port), "NODE_ENV": "production"}
        try:
            self._log_handle = _open_log()
            self._process = self._popen_factory(
                [str(binary)],
                env=env,
                stdout=self._log_handle,
                stderr=subprocess.STDOUT,
                cwd=str(binary.parent),
            )
        except OSError:
            logger.exception("Failed to spawn RSSHub binary %s", binary)
            self._process = None
            self._close_log()
            return False
        logger.info("RSSHub spawning on port %d (pid %s)", self._port, self._process.pid)
        deadline = self._monotonic() + self._startup_timeout
        while self._monotonic() < deadline:
            exit_code = self._process.poll()
            if exit_code is not None:
                logger.error(
                    "RSSHub exited during startup with code %s; see %s",
                    exit_code, _LOG_PATH,
                )
                self._process = None
                self._close_log()
                return False
            if self.is_alive():
                logger.info("RSSHub healthy at %s", self.base_url)
                return True
            self._sleep(_HEALTH_POLL_INTERVAL_SECONDS)
        logger.error("RSSHub not healthy within %.0fs; stopping it", self._startup_timeout)
        self.stop()
        return False

    def stop(self) -> None:
        """Terminate gracefully; force-kill after stop_timeout_seconds."""
        process, self._process = self._process, None
        if process is None:
            self._close_log()
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=self._stop_timeout)
            except subprocess.TimeoutExpired:
                logger.warning(
                    "RSSHub ignored terminate for %.0fs; killing", self._stop_timeout
                )
                process.kill()
                try:
                    process.wait(timeout=self._stop_timeout)
                except subprocess.TimeoutExpired:
                    logger.error("RSSHub process survived kill; it may leak")
        self._close_log()
        logger.info("RSSHub stopped")

    def is_alive(self) -> bool:
        """True when GET /healthz returns HTTP 200 (section 4.2)."""
        url = f"{self.base_url}{HEALTHZ_PATH}"
        try:
            with urllib.request.urlopen(
                url, timeout=_HEALTH_REQUEST_TIMEOUT_SECONDS
            ) as response:
                return bool(response.status == 200)
        except (urllib.error.URLError, OSError):
            return False

    def get_url(self, route: str) -> str:
        """Absolute URL for a route, e.g. get_url("/thepaper/featured")."""
        if not route.startswith("/"):
            route = "/" + route
        return f"{self.base_url}{route}"

    # -- internals ------------------------------------------------------------

    def _find_available_port(self) -> int:
        """Return the configured port, or the next free port above it.

        Raises RuntimeError when 100 consecutive ports are all occupied.
        """
        for offset in range(_MAX_PORT_ATTEMPTS):
            candidate = self._configured_port + offset
            if _port_is_free(candidate):
                if offset:
                    logger.warning(
                        "Port %d occupied; RSSHub will use %d",
                        self._configured_port, candidate,
                    )
                return candidate
        raise RuntimeError(
            f"No free port for RSSHub in range "
            f"[{self._configured_port}, {self._configured_port + _MAX_PORT_ATTEMPTS})"
        )

    def _get_binary_path(self) -> Path:
        """Platform binary under resources/ (section 5 layout)."""
        if self._binary_path is not None:
            return self._binary_path
        name = _BINARY_NAME_BY_PLATFORM.get(sys.platform)
        if name is None:
            raise RuntimeError(
                f"No embedded RSSHub binary for platform {sys.platform!r}"
            )
        return _RESOURCES_DIR / name

    def _close_log(self) -> None:
        """Close the redirected server log handle when one is open."""
        if self._log_handle is not None:
            try:
                self._log_handle.close()
            except OSError:
                logger.debug("Failed to close RSSHub log handle", exc_info=True)
            self._log_handle = None
