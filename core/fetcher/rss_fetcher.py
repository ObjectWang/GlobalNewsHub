"""Asynchronous RSS/Atom fetcher (PRD task P1.3).

- HTTP goes through ``core.utils.network.HttpClient`` (retry/timeout/UA
  compliance, task P1.4); parsing via feedparser (section 1.2).
- ``RateLimiter`` enforces FETCH_INTERVAL_SECONDS (3s, section 2.2). It is
  injectable so the orchestrator (P2.3) can hold one limiter per host:
  per-host politeness intervals with cross-host concurrency (section 1.3
  requires a full 30-source refresh within 5 seconds).
- On HTTP errors this module raises and lets the section 4.3 fallback
  chain react.
"""

from __future__ import annotations

import asyncio
import calendar
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import feedparser
from bs4 import BeautifulSoup

from core.constants import FETCH_INTERVAL_SECONDS, REQUEST_TIMEOUT
from core.models import Article
from core.utils.network import DEFAULT_USER_AGENT, HttpClient

logger = logging.getLogger(__name__)


class RateLimiter:
    """Enforce a minimum interval between ``wait()`` acquisitions.

    Clock and sleeper are injectable so tests can verify the interval
    without real sleeping.
    """

    def __init__(
        self,
        min_interval_seconds: float = FETCH_INTERVAL_SECONDS,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._min_interval = min_interval_seconds
        self._monotonic = monotonic
        self._sleep = sleep
        self._last_acquired_at: float | None = None

    async def wait(self) -> None:
        """Block until at least ``min_interval_seconds`` since the last call."""
        if self._last_acquired_at is not None:
            elapsed = self._monotonic() - self._last_acquired_at
            remaining = self._min_interval - elapsed
            if remaining > 0:
                await self._sleep(remaining)
        self._last_acquired_at = self._monotonic()


def _to_iso8601(value: Any) -> str | None:
    """Convert a feedparser ``struct_time`` (UTC) to an ISO8601 string."""
    if value is None:
        return None
    return datetime.fromtimestamp(calendar.timegm(value), tz=UTC).isoformat()


def _extract_content(entry: Any) -> str | None:
    contents = entry.get("content")
    if contents:
        return str(contents[0].get("value"))
    return None


def _extract_tags(entry: Any) -> list[str]:
    return [str(tag["term"]) for tag in entry.get("tags", []) if tag.get("term")]


def _extract_images(entry: Any) -> list[str]:
    urls: list[str] = []
    for media in entry.get("media_content", []):
        url = media.get("url")
        if url and str(media.get("type", "")).startswith("image"):
            urls.append(str(url))
    for enclosure in entry.get("enclosures", []):
        url = enclosure.get("href") or enclosure.get("url")
        if url and str(enclosure.get("type", "")).startswith("image"):
            urls.append(str(url))
    return urls


def _plain_length(html: str) -> int:
    """Character count of the text content of an HTML fragment."""
    return len(BeautifulSoup(html, "lxml").get_text())


def parse_feed(
    payload: str | bytes,
    *,
    source_media: str,
    category: str = "other",
    region: str = "unknown",
    language: str = "zh-CN",
) -> list[Article]:
    """Parse an RSS/Atom payload into Articles (pure function, mock-friendly).

    Entries without title or link are skipped (``source_url``/``title`` are
    NOT NULL in the section 2.3 schema). ``category``/``region`` carry the
    source-level hints; the classifier may override them later (section 3.2).
    """
    parsed = feedparser.parse(payload)
    if parsed.bozo and not parsed.entries:
        logger.warning("Malformed feed payload for %s: %s",
                       source_media, parsed.get("bozo_exception"))
    fetched_at = datetime.now(UTC).isoformat()
    articles: list[Article] = []
    for entry in parsed.entries:
        link = entry.get("link") or entry.get("id")
        title = entry.get("title")
        if not link or not title:
            logger.warning("Skipping entry without title/link from %s", source_media)
            continue
        content = _extract_content(entry)
        summary = entry.get("summary")
        articles.append(
            Article(
                id=uuid4().hex,
                title=str(title),
                summary=str(summary) if summary else None,
                content=content,
                source_media=source_media,
                source_url=str(link),
                category=category,
                region=region,
                published_at=_to_iso8601(
                    entry.get("published_parsed") or entry.get("updated_parsed")
                ),
                fetched_at=fetched_at,
                tags=_extract_tags(entry),
                language=language,
                word_count=_plain_length(content or str(summary or "")),
                image_urls=_extract_images(entry),
            )
        )
    return articles


class RSSFetcher:
    """Fetch feeds over HTTP with rate limiting, then parse them."""

    def __init__(
        self,
        *,
        rate_limiter: RateLimiter | None = None,
        http_client: HttpClient | None = None,
        timeout_seconds: float = REQUEST_TIMEOUT,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self._rate_limiter = rate_limiter or RateLimiter()
        self._http = http_client or HttpClient(
            timeout_seconds=timeout_seconds, user_agent=user_agent
        )

    async def fetch(
        self,
        url: str,
        *,
        source_media: str,
        category: str = "other",
        region: str = "unknown",
        language: str = "zh-CN",
    ) -> list[Article]:
        """Fetch one feed URL (rate-limited) and return parsed Articles.

        Raises aiohttp.ClientError on HTTP/connection failures once the
        HttpClient retry policy is exhausted; the caller (fetch
        orchestrator, section 4.3) owns the fallback chain.
        """
        await self._rate_limiter.wait()
        payload = await self._http.get_text(url)
        logger.info("Fetched %d bytes from %s", len(payload), url)
        return parse_feed(
            payload,
            source_media=source_media,
            category=category,
            region=region,
            language=language,
        )
