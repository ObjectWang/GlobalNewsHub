"""Tests for core.utils.network (PRD task P1.4).

Acceptance: retry policy, timeout wiring and User-Agent compliance.
All HTTP is stubbed; no real network or real sleeping is involved.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest

from core.constants import MAX_RETRY, REQUEST_TIMEOUT
from core.utils.network import DEFAULT_USER_AGENT, HttpClient


class _FakeResponse:
    """Mimics an aiohttp response context manager.

    Connection-level errors raise on __aenter__ (like aiohttp does when the
    request itself fails); HTTP status errors raise from raise_for_status.
    """

    def __init__(self, payload: str = "", error: Exception | None = None) -> None:
        self._payload = payload
        self._error = error

    async def __aenter__(self) -> _FakeResponse:
        if self._error is not None and not isinstance(self._error, aiohttp.ClientResponseError):
            raise self._error
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    def raise_for_status(self) -> None:
        if isinstance(self._error, aiohttp.ClientResponseError):
            raise self._error

    async def text(self) -> str:
        return self._payload


def _session_factory(script: list[str | Exception],
                     captured: list[dict[str, Any]]) -> Any:
    """Build a ClientSession stand-in whose get() pops scripted outcomes."""

    class _Session:
        def __init__(self, **kwargs: Any) -> None:
            captured.append(kwargs)

        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *args: object) -> bool:
            return False

        def get(self, url: str, **kwargs: Any) -> _FakeResponse:
            captured.append(kwargs)
            outcome = script.pop(0)
            if isinstance(outcome, Exception):
                return _FakeResponse(error=outcome)
            return _FakeResponse(payload=outcome)

    return _Session


def _response_error(status: int) -> aiohttp.ClientResponseError:
    request_info = SimpleNamespace(real_url="https://example.com/feed")
    return aiohttp.ClientResponseError(
        request_info=request_info, history=(), status=status
    )


async def test_get_text_success_sends_ua_and_timeout() -> None:
    captured: list[dict[str, Any]] = []
    client = HttpClient(session_factory=_session_factory(["payload-body"], captured))
    assert await client.get_text("https://example.com/feed") == "payload-body"
    session_kwargs, get_kwargs = captured
    # UA compliance (section 9).
    assert session_kwargs["headers"]["User-Agent"] == DEFAULT_USER_AGENT
    # Timeout wiring (REQUEST_TIMEOUT, section 2.2).
    assert get_kwargs["timeout"].total == REQUEST_TIMEOUT


async def test_retry_on_connection_error_then_success() -> None:
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    script: list[str | Exception] = [
        aiohttp.ClientConnectionError("reset"),
        aiohttp.ClientConnectionError("reset"),
        "ok",
    ]
    client = HttpClient(session_factory=_session_factory(script, []), sleep=fake_sleep)
    assert await client.get_text("https://example.com/feed") == "ok"
    assert len(script) == 0
    assert sleeps == [1.0, 2.0]  # exponential backoff between attempts


async def test_retry_exhausted_raises_after_max_retry() -> None:
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    attempts = 1 + MAX_RETRY
    script: list[str | Exception] = [aiohttp.ClientConnectionError("down")] * attempts
    client = HttpClient(session_factory=_session_factory(script, []), sleep=fake_sleep)
    with pytest.raises(aiohttp.ClientConnectionError):
        await client.get_text("https://example.com/feed")
    assert script == []  # exactly 1 initial attempt + MAX_RETRY retries
    assert sleeps == [1.0, 2.0, 4.0]


async def test_no_retry_on_permanent_4xx() -> None:
    script: list[str | Exception] = [_response_error(404)]
    client = HttpClient(session_factory=_session_factory(script, []),
                        sleep=_never_called)
    with pytest.raises(aiohttp.ClientResponseError):
        await client.get_text("https://example.com/missing")
    assert script == []  # single attempt only


async def test_retry_on_transient_503() -> None:
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    script: list[str | Exception] = [_response_error(503), "ok"]
    client = HttpClient(session_factory=_session_factory(script, []), sleep=fake_sleep)
    assert await client.get_text("https://example.com/feed") == "ok"
    assert sleeps == [1.0]


async def test_timeout_is_retryable() -> None:
    sleeps: list[float] = []
    script: list[str | Exception] = [TimeoutError("slow"), "ok"]

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    client = HttpClient(session_factory=_session_factory(script, []), sleep=fake_sleep)
    assert await client.get_text("https://example.com/feed") == "ok"
    assert len(sleeps) == 1


async def _never_called(delay: float) -> None:
    raise AssertionError(f"sleep should not be called, got {delay}")
