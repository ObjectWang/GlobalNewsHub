"""Shared asynchronous HTTP utilities (PRD task P1.4).

Wraps aiohttp with the compliance rules of sections 1.2, 2.2 and 9:

- User-Agent header on every request (section 9).
- Per-request timeout of REQUEST_TIMEOUT seconds (section 2.2).
- Up to MAX_RETRY retries with exponential backoff (section 2.2), but
  only for transient failures: connection errors, timeouts and 5xx/408/429
  responses. Permanent 4xx errors are raised immediately.

Rate limiting lives in ``core.fetcher.rss_fetcher.RateLimiter`` (per-host,
section 2.2 FETCH_INTERVAL_SECONDS). robots.txt handling is a section 9
red line and is enforced when spider.py lands.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

from core.constants import MAX_RETRY, REQUEST_TIMEOUT

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = "GlobalNewsHub/1.0 (+contact@email.com)"  # section 9

_RETRYABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
_BACKOFF_BASE_SECONDS = 1.0
_BACKOFF_CAP_SECONDS = 30.0


def _is_retryable(exc: Exception) -> bool:
    """Classify transient (worth retrying) vs permanent failures."""
    if isinstance(exc, aiohttp.ClientResponseError):
        return exc.status in _RETRYABLE_STATUSES
    return isinstance(exc, (aiohttp.ClientConnectionError, asyncio.TimeoutError))


class HttpClient:
    """Minimal async HTTP client with retry/timeout/UA compliance.

    ``sleep`` and ``session_factory`` are injectable for tests. A fresh
    session is created per attempt; callers fetch politely (>= 3 s per
    host) so connection reuse is not on the hot path.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = REQUEST_TIMEOUT,
        max_retry: int = MAX_RETRY,
        user_agent: str = DEFAULT_USER_AGENT,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        session_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._max_retry = max_retry
        self._headers = {"User-Agent": user_agent}
        self._sleep = sleep
        self._session_factory = session_factory or aiohttp.ClientSession

    async def get_text(self, url: str) -> str:
        """GET ``url`` and return the decoded body.

        Makes 1 initial attempt plus up to MAX_RETRY retries on transient
        failures, with exponential backoff (1s, 2s, 4s, ... capped at 30s).
        Re-raises the last error when retries are exhausted, and raises
        permanent (4xx) errors immediately.
        """
        last_error: Exception | None = None
        delay = _BACKOFF_BASE_SECONDS
        for attempt in range(1 + self._max_retry):
            try:
                async with self._session_factory(headers=self._headers) as session:
                    async with session.get(url, timeout=self._timeout) as response:
                        response.raise_for_status()
                        body: str = await response.text()
                        return body
            except Exception as exc:
                if not _is_retryable(exc):
                    raise
                last_error = exc
                if attempt < self._max_retry:
                    logger.warning(
                        "GET %s failed (attempt %d/%d): %s; retrying in %.0fs",
                        url, attempt + 1, 1 + self._max_retry, exc, delay,
                    )
                    await self._sleep(delay)
                    delay = min(delay * 2, _BACKOFF_CAP_SECONDS)
        assert last_error is not None
        logger.error("GET %s failed after %d attempts", url, 1 + self._max_retry)
        raise last_error
