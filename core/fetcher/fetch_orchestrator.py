"""Dual-channel fault-tolerant fetch orchestr (PRD task P2.3).

Implements the section 4.3 degradation chain:

  rss source    -> direct native RSS URL;
                   on failure: fallback_rsshub route via the embedded
                   RSSHub; on failure: the public rsshub.app instance;
                   on failure: the source is reported unavailable.
  rsshub source -> embedded RSSHub route; on failure the same public
                   fallback chain.

The orchestrator only *fetches*; marking a source unavailable after
repeated failures belongs to the health tracker (section 4.4,
core.fetcher.health_check), which consumes FetchResult.ok.

The two channel primitives (direct RSS fetch, RSSHub route fetch) are
injectable so tests can simulate the whole chain offline. The defaults
reuse core.fetcher.rss_fetcher with one RateLimiter per remote host:
politeness (FETCH_INTERVAL_SECONDS, sections 2.2/4.4) applies to
external origins only — the embedded RSSHub is our own localhost server
and must not be throttled, otherwise the 5-second full refresh target
(section 1.3) is unreachable with many rsshub sources.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, TypeAlias
from urllib.parse import urlparse

import yaml

from core.fetcher.rss_fetcher import RateLimiter, RSSFetcher
from core.models import Article
from core.utils.network import HttpClient

logger = logging.getLogger(__name__)

SOURCE_TYPE_RSS: str = "rss"
SOURCE_TYPE_RSSHUB: str = "rsshub"

CHANNEL_RSS: str = "rss"
CHANNEL_LOCAL_RSSHUB: str = "local_rsshub"
CHANNEL_PUBLIC_RSSHUB: str = "public_rsshub"

PUBLIC_FALLBACK_URL: str = "https://rsshub.app"  # settings.yaml rsshub.public_fallback_url

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


@dataclass(frozen=True)
class SourceConfig:
    """One news source; field contract documented in config/sources.yaml."""

    id: str
    name: str
    type: str                                   # "rss" | "rsshub"
    url: str | None = None                      # type=rss: native feed URL
    fallback_rsshub: str | None = None          # type=rss: local route fallback
    route: str | None = None                    # type=rsshub: local route
    category: str = "other"                     # initial hint, classifier may override
    region: str = "unknown"                     # initial hint (section 3.2 priority 1)
    language: str = "zh-CN"
    enabled: bool = True


def load_sources(path: Path) -> list[SourceConfig]:
    """Parse config/sources.yaml; raise ValueError on invalid entries.

    Validation runs at load time so misconfigured sources surface at
    startup instead of mid-refresh.
    """
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    sources: list[SourceConfig] = []
    for raw in data.get("sources", []):
        source = SourceConfig(
            id=str(raw["id"]),
            name=str(raw["name"]),
            type=str(raw["type"]),
            url=raw.get("url"),
            fallback_rsshub=raw.get("fallback_rsshub"),
            route=raw.get("route"),
            category=str(raw.get("category", "other")),
            region=str(raw.get("region", "unknown")),
            language=str(raw.get("language", "zh-CN")),
            enabled=bool(raw.get("enabled", True)),
        )
        _validate_source(source)
        sources.append(source)
    return sources


def _validate_source(source: SourceConfig) -> None:
    """Reject unknown types and missing required fields (section 4.3)."""
    if source.type not in (SOURCE_TYPE_RSS, SOURCE_TYPE_RSSHUB):
        raise ValueError(f"source {source.id}: unknown type {source.type!r}")
    if source.type == SOURCE_TYPE_RSS and not source.url:
        raise ValueError(f"source {source.id}: rss source requires url")
    if source.type == SOURCE_TYPE_RSSHUB and not source.route:
        raise ValueError(f"source {source.id}: rsshub source requires route")


@dataclass(frozen=True)
class FetchResult:
    """Outcome of one source fetch through the section 4.3 chain.

    channel is None when every channel failed; errors holds one
    "channel: message" entry per failed attempt, in chain order.
    """

    source_id: str
    articles: tuple[Article, ...]
    channel: str | None
    errors: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        """True when some channel served the source."""
        return self.channel is not None


class RSSHubLike(Protocol):
    """Structural view of RSSHubManager used by the orchestrator."""

    def is_alive(self) -> bool: ...

    def get_url(self, route: str) -> str: ...


RssFetchFn: TypeAlias = Callable[[SourceConfig], Awaitable[list[Article]]]
RssHubFetchFn: TypeAlias = Callable[[str, SourceConfig], Awaitable[list[Article]]]


class FetchOrchestrator:
    """Runs the section 4.3 fallback chain for each configured source."""

    def __init__(
        self,
        rsshub: RSSHubLike | None = None,
        *,
        public_fallback_url: str = PUBLIC_FALLBACK_URL,
        rss_fetch: RssFetchFn | None = None,
        rsshub_fetch: RssHubFetchFn | None = None,
        http_client: HttpClient | None = None,
        max_concurrent_fetches: int = 5,
    ) -> None:
        """Wire channels; rss_fetch/rsshub_fetch are injectable for tests."""
        self._rsshub = rsshub
        self._public_fallback_url = public_fallback_url.rstrip("/")
        self._max_concurrent = max_concurrent_fetches
        self._http = http_client or HttpClient()
        self._rss_fetch: RssFetchFn = rss_fetch or self._default_rss_fetch
        self._rsshub_fetch: RssHubFetchFn = rsshub_fetch or self._default_rsshub_fetch
        self._fetchers_by_host: dict[str, RSSFetcher] = {}

    # -- public API -----------------------------------------------------------

    async def fetch_source(self, source: SourceConfig) -> FetchResult:
        """Fetch one source through the degradation chain (section 4.3)."""
        errors: list[str] = []
        if source.type == SOURCE_TYPE_RSS:
            try:
                articles = await self._rss_fetch(source)
                return FetchResult(source.id, tuple(articles), CHANNEL_RSS)
            except Exception as exc:
                errors.append(f"{CHANNEL_RSS}: {exc}")
                logger.warning("Direct RSS failed for %s: %s", source.id, exc)
                if source.fallback_rsshub:
                    return await self._rsshub_chain(source, source.fallback_rsshub, errors)
                logger.error(
                    "Source %s unavailable: direct RSS failed and no fallback configured",
                    source.id,
                )
                return FetchResult(source.id, (), None, tuple(errors))
        if source.type == SOURCE_TYPE_RSSHUB:
            assert source.route is not None  # guaranteed by load_sources
            return await self._rsshub_chain(source, source.route, errors)
        raise ValueError(f"source {source.id}: unknown type {source.type!r}")

    async def fetch_all(
        self, sources: Sequence[SourceConfig]
    ) -> dict[str, FetchResult]:
        """Fetch every source concurrently, bounded by max_concurrent_fetches.

        Concurrency across distinct hosts plus per-host 3s politeness is
        what keeps a 30-source refresh within the 5-second target
        (section 1.3; settings scheduler.max_concurrent_fetches).
        """
        semaphore = asyncio.Semaphore(self._max_concurrent)

        async def _run(source: SourceConfig) -> tuple[str, FetchResult]:
            async with semaphore:
                return source.id, await self.fetch_source(source)

        pairs = await asyncio.gather(*(_run(source) for source in sources))
        return dict(pairs)

    # -- fallback chain ---------------------------------------------------------

    async def _rsshub_chain(
        self, source: SourceConfig, route: str, errors: list[str]
    ) -> FetchResult:
        """Local RSSHub first, then the public instance (section 4.3)."""
        if self._rsshub is not None and self._rsshub.is_alive():
            url = self._rsshub.get_url(route)
            try:
                articles = await self._rsshub_fetch(url, source)
                return FetchResult(source.id, tuple(articles), CHANNEL_LOCAL_RSSHUB, tuple(errors))
            except Exception as exc:
                errors.append(f"{CHANNEL_LOCAL_RSSHUB}: {exc}")
                logger.warning(
                    "Local RSSHub failed for %s (%s); trying public fallback",
                    source.id, exc,
                )
        else:
            logger.info(
                "Embedded RSSHub not running; %s goes straight to public fallback",
                source.id,
            )
        public_url = f"{self._public_fallback_url}/{route.lstrip('/')}"
        try:
            articles = await self._rsshub_fetch(public_url, source)
        except Exception as exc:
            errors.append(f"{CHANNEL_PUBLIC_RSSHUB}: {exc}")
            logger.error("Source %s unavailable: all channels failed", source.id)
            return FetchResult(source.id, (), None, tuple(errors))
        logger.warning(
            "Source %s served by PUBLIC RSSHub fallback %s (section 4.1 degraded mode)",
            source.id, public_url,
        )
        return FetchResult(source.id, tuple(articles), CHANNEL_PUBLIC_RSSHUB, tuple(errors))

    # -- default channel implementations -------------------------------------

    async def _default_rss_fetch(self, source: SourceConfig) -> list[Article]:
        """Direct native RSS through the per-host rate-limited fetcher."""
        assert source.url is not None
        fetcher = self._fetcher_for(source.url)
        return await fetcher.fetch(
            source.url,
            source_media=source.name,
            category=source.category,
            region=source.region,
            language=source.language,
        )

    async def _default_rsshub_fetch(self, url: str, source: SourceConfig) -> list[Article]:
        """Fetch an RSSHub route (local or public) and parse it as RSS."""
        fetcher = self._fetcher_for(url)
        return await fetcher.fetch(
            url,
            source_media=source.name,
            category=source.category,
            region=source.region,
            language=source.language,
        )

    def _fetcher_for(self, url: str) -> RSSFetcher:
        """One RSSFetcher per host; localhost is never rate-limited."""
        host = urlparse(url).netloc or url
        fetcher = self._fetchers_by_host.get(host)
        if fetcher is None:
            limiter = (
                RateLimiter(0.0) if self._is_local_host(host) else RateLimiter()
            )
            fetcher = RSSFetcher(rate_limiter=limiter, http_client=self._http)
            self._fetchers_by_host[host] = fetcher
        return fetcher

    @staticmethod
    def _is_local_host(host: str) -> bool:
        """True for the embedded server's host (localhost[:port] forms)."""
        name = host.split(":", 1)[0]
        return name in _LOCAL_HOSTS
