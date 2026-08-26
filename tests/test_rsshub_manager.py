"""Tests for core.rsshub.manager (PRD task P2.2).

Acceptance: start -> healthz -> stop lifecycle works. Unit tests run the
manager against a fake subprocess that serves /healthz on a real local
socket, so the readiness polling, port discovery and stop semantics are
exercised end to end without the real RSSHub binary. The final test runs
against the real pkg binary produced by scripts/build_rsshub.sh (P2.1)
when present, and skips otherwise.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from core.rsshub.manager import RSSHubManager


def _free_port() -> int:
    """Ask the OS for a currently free TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _HealthzHandler(BaseHTTPRequestHandler):
    """Serves HTTP 200 for every GET (healthz and route lookalikes)."""

    def do_GET(self) -> None:  # noqa: N802 - http.server API name
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:  # silence request log
        pass


class _FakeRSSHubProcess:
    """Stand-in for subprocess.Popen backed by a real HTTP server.

    Reads PORT from the environment exactly like the packaged RSSHub
    binary does. Options simulate failure modes: 'exit_code' exits
    immediately, 'serve_healthz=False' starts no server (dead process),
    'hang_on_terminate' ignores terminate() so stop() must kill.
    """

    def __init__(
        self,
        args: list[str],
        *,
        env: dict[str, str] | None = None,
        serve_healthz: bool = True,
        exit_code: int | None = None,
        hang_on_terminate: bool = False,
        **kwargs: Any,
    ) -> None:
        self.args = args
        self.pid = 4242  # fake but present, like subprocess.Popen.pid
        self.env = dict(env or {})
        self.kwargs = kwargs
        self.terminated = False
        self.killed = False
        self._hang = hang_on_terminate
        self._returncode: int | None = None
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        if exit_code is not None:
            self._returncode = exit_code
        elif serve_healthz:
            port = int(self.env["PORT"])
            self._server = ThreadingHTTPServer(("127.0.0.1", port), _HealthzHandler)
            self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
            self._thread.start()

    def poll(self) -> int | None:
        return self._returncode

    def terminate(self) -> None:
        self.terminated = True
        if not self._hang:
            self._shutdown(0)

    def kill(self) -> None:
        self.killed = True
        self._shutdown(-9)

    def wait(self, timeout: float | None = None) -> int:
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                raise subprocess.TimeoutExpired(self.args, timeout)
        if self._returncode is None:
            self._returncode = 0
        return self._returncode

    def _shutdown(self, code: int) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        self._returncode = code


class _FakePopenFactory:
    """Callable compatible with RSSHubManager's popen_factory argument."""

    def __init__(
        self,
        *,
        serve_healthz: bool = True,
        exit_code: int | None = None,
        hang_on_terminate: bool = False,
    ) -> None:
        self.serve_healthz = serve_healthz
        self.exit_code = exit_code
        self.hang_on_terminate = hang_on_terminate
        self.spawned: list[_FakeRSSHubProcess] = []

    def __call__(self, args: list[str], **kwargs: Any) -> _FakeRSSHubProcess:
        process = _FakeRSSHubProcess(
            args,
            serve_healthz=self.serve_healthz,
            exit_code=self.exit_code,
            hang_on_terminate=self.hang_on_terminate,
            **kwargs,
        )
        self.spawned.append(process)
        return process


def _make_manager(
    tmp_path: Path,
    factory: _FakePopenFactory,
    *,
    port: int | None = None,
    startup_timeout_seconds: float = 5.0,
    stop_timeout_seconds: float = 5.0,
) -> RSSHubManager:
    """Manager wired to a fake binary file and the fake popen factory."""
    binary = tmp_path / "rsshub-server.exe"
    binary.write_bytes(b"fake-binary")
    return RSSHubManager(
        port=port if port is not None else _free_port(),
        binary_path=binary,
        startup_timeout_seconds=startup_timeout_seconds,
        stop_timeout_seconds=stop_timeout_seconds,
        popen_factory=factory,
    )


def test_start_healthz_stop_lifecycle(tmp_path: Path) -> None:
    factory = _FakePopenFactory()
    manager = _make_manager(tmp_path, factory)
    try:
        assert manager.start() is True
        assert manager.is_alive() is True
        process = factory.spawned[0]
        # Environment contract: PORT + production mode (route manifest).
        assert process.env["PORT"] == str(manager.port)
        assert process.env["NODE_ENV"] == "production"
        assert manager.get_url("/zhihu/hotlist") == (
            f"http://localhost:{manager.port}/zhihu/hotlist"
        )
    finally:
        manager.stop()
    assert manager.is_alive() is False
    assert factory.spawned[0].terminated is True


def test_start_is_idempotent_while_healthy(tmp_path: Path) -> None:
    factory = _FakePopenFactory()
    manager = _make_manager(tmp_path, factory)
    try:
        assert manager.start() is True
        assert manager.start() is True
        assert len(factory.spawned) == 1  # no second process spawned
    finally:
        manager.stop()


def test_start_fails_when_binary_missing(tmp_path: Path) -> None:
    factory = _FakePopenFactory()
    manager = RSSHubManager(
        port=_free_port(),
        binary_path=tmp_path / "does-not-exist.exe",
        popen_factory=factory,
    )
    assert manager.start() is False
    assert factory.spawned == []


def test_start_fails_when_healthz_never_ready(tmp_path: Path) -> None:
    factory = _FakePopenFactory(serve_healthz=False)
    manager = _make_manager(tmp_path, factory, startup_timeout_seconds=0.6)
    assert manager.start() is False
    # The half-started process must be cleaned up before returning.
    assert factory.spawned[0].terminated is True
    assert manager.is_alive() is False


def test_start_fails_when_process_exits_early(tmp_path: Path) -> None:
    factory = _FakePopenFactory(exit_code=1)
    manager = _make_manager(tmp_path, factory, startup_timeout_seconds=2.0)
    assert manager.start() is False
    assert manager.is_alive() is False


def test_stop_kills_when_terminate_times_out(tmp_path: Path) -> None:
    factory = _FakePopenFactory(hang_on_terminate=True)
    manager = _make_manager(tmp_path, factory, stop_timeout_seconds=0.3)
    assert manager.start() is True
    manager.stop()
    process = factory.spawned[0]
    assert process.terminated is True  # graceful attempt happened first
    assert process.killed is True      # then forced kill
    assert manager.is_alive() is False


def test_find_available_port_skips_occupied(tmp_path: Path) -> None:
    occupied = _free_port()
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", occupied))
    blocker.listen(1)
    try:
        factory = _FakePopenFactory()
        manager = _make_manager(tmp_path, factory, port=occupied)
        found = manager._find_available_port()
        assert found > occupied
        assert manager.start() is True
        try:
            assert manager.port == found
            assert manager.is_alive() is True
        finally:
            manager.stop()
    finally:
        blocker.close()


def test_get_url_adds_leading_slash(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path, _FakePopenFactory())
    assert manager.get_url("weibo/search/hot") == (
        f"http://localhost:{manager.port}/weibo/search/hot"
    )


def test_unsupported_platform_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "freebsd")
    manager = RSSHubManager(port=_free_port())
    with pytest.raises(RuntimeError, match="freebsd"):
        manager._get_binary_path()


def test_real_binary_start_healthz_stop() -> None:
    """P2.2 acceptance against the real P2.1 binary (skipped until built)."""
    manager = RSSHubManager(port=_free_port(), startup_timeout_seconds=45.0)
    binary = manager._get_binary_path()
    if not binary.is_file() or binary.stat().st_size == 0:
        pytest.skip("real RSSHub binary not present yet (run scripts/build_rsshub.sh)")
    assert manager.start() is True
    try:
        assert manager.is_alive() is True
    finally:
        manager.stop()
    assert manager.is_alive() is False


def test_spawn_suppresses_console_window_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """User-reported flash: console binary must spawn without a window."""
    factory = _FakePopenFactory()
    manager = _make_manager(tmp_path, factory)
    monkeypatch.setattr(sys, "platform", "win32")
    assert manager.start() is True
    try:
        kwargs = factory.spawned[0].kwargs
        assert kwargs.get("creationflags") == 0x08000000  # CREATE_NO_WINDOW
    finally:
        manager.stop()


def test_spawn_omits_creationflags_off_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = _FakePopenFactory()
    manager = _make_manager(tmp_path, factory)
    (tmp_path / "rsshub-server-linux").write_bytes(b"fake")
    monkeypatch.setattr(sys, "platform", "linux")
    assert manager.start() is True
    try:
        assert "creationflags" not in factory.spawned[0].kwargs
    finally:
        manager.stop()
