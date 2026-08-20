"""Tests for core.fetcher.rss_fetcher (PRD task P1.3).

Acceptance: mock feeds parse into Article correctly, and the 3-second
rate limit (FETCH_INTERVAL_SECONDS, section 2.2) is actually enforced.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import aiohttp
import pytest

from core.constants import FETCH_INTERVAL_SECONDS, REQUEST_TIMEOUT
from core.fetcher.rss_fetcher import RateLimiter, RSSFetcher, parse_feed
from core.models import Article

SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/"
     xmlns:media="http://search.yahoo.com/mrss/">
  <channel>
    <title>Mock News</title>
    <item>
      <title>China economy grows steadily</title>
      <link>https://example.com/news/1</link>
      <description>Summary one</description>
      <content:encoded><![CDATA[<p>Full content one</p>]]></content:encoded>
      <pubDate>Wed, 19 Aug 2026 08:00:00 +0800</pubDate>
      <category>经济</category>
      <category>中国</category>
      <media:content url="https://example.com/a.jpg" type="image/jpeg" medium="image"/>
      <enclosure url="https://example.com/b.jpg" type="image/png" length="1024"/>
    </item>
    <item>
      <title>Second item</title>
      <link>https://example.com/news/2</link>
      <pubDate>Thu, 20 Aug 2026 01:00:00 +0000</pubDate>
    </item>
    <item>
      <title>No link item</title>
    </item>
  </channel>
</rss>
"""

SAMPLE_ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Mock Atom</title>
  <entry>
    <title>Atom entry</title>
    <link href="https://example.com/atom/1"/>
    <id>https://example.com/atom/1</id>
    <updated>2026-08-20T02:00:00Z</updated>
    <summary>Atom summary</summary>
  </entry>
</feed>
"""


def _clock(values: list[float]) -> Callable[[], float]:
    """Fake monotonic clock popping scripted values."""
    iterator = iter(values)
    return lambda: next(iterator)


def _sleep_recorder(record: list[float]) -> Callable[[float], Any]:
    async def _sleep(delay: float) -> None:
        record.append(delay)

    return _sleep


def test_parse_rss_mock_source() -> None:
    articles = parse_feed(
        SAMPLE_RSS, source_media="测试源", category="economy", region="china"
    )
    assert len(articles) == 2  # the link-less third item is skipped
    first, second = articles
    assert isinstance(first, Article)
    assert first.title == "China economy grows steadily"
    assert first.source_url == "https://example.com/news/1"
    assert first.summary == "Summary one"
    assert first.content == "<p>Full content one</p>"
    assert first.published_at == "2026-08-19T00:00:00+00:00"  # +0800 -> UTC
    assert first.tags == ["经济", "中国"]
    assert first.image_urls == ["https://example.com/a.jpg", "https://example.com/b.jpg"]
    assert first.source_media == "测试源"
    assert first.category == "economy"
    assert first.region == "china"
    assert first.language == "zh-CN"
    assert first.word_count > 0
    assert first.fetched_at is not None
    assert second.title == "Second item"
    assert second.published_at == "2026-08-20T01:00:00+00:00"
    assert second.content is None
    assert second.tags == []
    assert second.image_urls == []


def test_parse_atom_mock_source() -> None:
    articles = parse_feed(SAMPLE_ATOM, source_media="Atom Source")
    assert len(articles) == 1
    entry = articles[0]
    assert entry.title == "Atom entry"
    assert entry.source_url == "https://example.com/atom/1"
    assert entry.summary == "Atom summary"
    assert entry.published_at == "2026-08-20T02:00:00+00:00"
    assert entry.category == "other"  # default hint
    assert entry.region == "unknown"


def test_rate_limiter_first_call_does_not_sleep() -> None:
    sleeps: list[float] = []
    limiter = RateLimiter(FETCH_INTERVAL_SECONDS, monotonic=_clock([5.0]),
                          sleep=_sleep_recorder(sleeps))
    asyncio.run(limiter.wait())
    assert sleeps == []


def test_rate_limiter_enforces_min_interval() -> None:
    """Second call 1.0s after the first must sleep the remaining 2.0s."""
    sleeps: list[float] = []
    limiter = RateLimiter(FETCH_INTERVAL_SECONDS, monotonic=_clock([0.0, 1.0, 3.5]),
                          sleep=_sleep_recorder(sleeps))
    asyncio.run(limiter.wait())
    asyncio.run(limiter.wait())
    assert sleeps == [pytest.approx(FETCH_INTERVAL_SECONDS - 1.0)]


class _FakeResponse:
    def __init__(self, payload: str, *, fail: bool = False) -> None:
        self._payload = payload
        self._fail = fail

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    def raise_for_status(self) -> None:
        if self._fail:
            raise aiohttp.ClientError("HTTP 500")

    async def text(self) -> str:
        return self._payload


class _FakeSession:
    """Stand-in for aiohttp.ClientSession capturing constructor kwargs."""

    last_kwargs: dict[str, Any] = {}
    fail: bool = False

    def __init__(self, **kwargs: Any) -> None:
        _FakeSession.last_kwargs = kwargs

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        _FakeSession.last_kwargs.setdefault("gets", []).append((url, kwargs))
        return _FakeResponse(SAMPLE_RSS, fail=_FakeSession.fail)


async def test_fetch_applies_rate_limit_ua_and_parses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    limiter = RateLimiter(FETCH_INTERVAL_SECONDS, monotonic=lambda: 0.0,
                          sleep=_sleep_recorder(sleeps))
    _FakeSession.fail = False
    monkeypatch.setattr(aiohttp, "ClientSession", _FakeSession)
    fetcher = RSSFetcher(rate_limiter=limiter)

    articles = await fetcher.fetch("https://example.com/feed", source_media="测试源")
    await fetcher.fetch("https://example.com/feed", source_media="测试源")

    assert [a.title for a in articles][:1] == ["China economy grows steadily"]
    assert sleeps == [pytest.approx(FETCH_INTERVAL_SECONDS)]  # second fetch waited 3s
    headers = _FakeSession.last_kwargs["headers"]
    assert headers["User-Agent"] == "GlobalNewsHub/1.0 (+contact@email.com)"
    _, get_kwargs = _FakeSession.last_kwargs["gets"][0]
    assert get_kwargs["timeout"].total == REQUEST_TIMEOUT


async def test_fetch_http_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTP failures surface as exceptions so §4.3 fallback chain can react."""
    _FakeSession.fail = True
    monkeypatch.setattr(aiohttp, "ClientSession", _FakeSession)
    fetcher = RSSFetcher(rate_limiter=RateLimiter(0.0))
    with pytest.raises(aiohttp.ClientError):
        await fetcher.fetch("https://example.com/broken", source_media="测试源")
