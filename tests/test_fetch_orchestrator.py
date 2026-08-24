"""Tests for core.fetcher.fetch_orchestrator (PRD task P2.3).

Acceptance: the section 4.3 degradation chain is fully covered with
simulated channels — no network access. Each test scripts the outcomes
of the two channel primitives (direct RSS fetch, RSSHub route fetch)
and asserts which channel ultimately served the articles.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.fetcher.fetch_orchestrator import (
    CHANNEL_LOCAL_RSSHUB,
    CHANNEL_PUBLIC_RSSHUB,
    CHANNEL_RSS,
    FetchOrchestrator,
    SourceConfig,
    load_sources,
)
from core.models import Article

_FAIL = ConnectionError("channel down")


def _article(url: str) -> Article:
    return Article(
        id="a-" + url,
        title="title",
        summary=None,
        content=None,
        source_media="媒体",
        source_url=url,
        category="other",
        region="unknown",
        published_at=None,
        fetched_at="2026-08-24T00:00:00+00:00",
        tags=[],
    )


def _rss_source(**overrides: object) -> SourceConfig:
    kwargs: dict[str, object] = {
        "id": "bbc_world",
        "name": "BBC World",
        "type": "rss",
        "url": "https://feeds.example.com/world.xml",
        "fallback_rsshub": None,
        "route": None,
    }
    kwargs.update(overrides)
    return SourceConfig(**kwargs)  # type: ignore[arg-type]


def _rsshub_source(**overrides: object) -> SourceConfig:
    kwargs: dict[str, object] = {
        "id": "zhihu_hotlist",
        "name": "知乎热榜",
        "type": "rsshub",
        "url": None,
        "fallback_rsshub": None,
        "route": "/zhihu/hotlist",
    }
    kwargs.update(overrides)
    return SourceConfig(**kwargs)  # type: ignore[arg-type]


class _FakeRSSHub:
    """Duck-typed stand-in for RSSHubManager (is_alive + get_url)."""

    def __init__(self, alive: bool = True) -> None:
        self.alive = alive
        self.routes: list[str] = []

    def is_alive(self) -> bool:
        return self.alive

    def get_url(self, route: str) -> str:
        if not route.startswith("/"):
            route = "/" + route
        self.routes.append(route)
        return f"http://localhost:1200{route}"


class _Scripts:
    """Records channel calls and replays scripted outcomes."""

    def __init__(self, rss: list[object], rsshub: list[object]) -> None:
        self.rss = rss
        self.rsshub = rsshub
        self.calls: list[str] = []

    async def rss_fetch(self, source: SourceConfig) -> list[Article]:
        self.calls.append(CHANNEL_RSS)
        outcome = self.rss.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return list(outcome)  # type: ignore[arg-type]

    async def rsshub_fetch(self, url: str, source: SourceConfig) -> list[Article]:
        channel = (
            CHANNEL_LOCAL_RSSHUB if "localhost" in url else CHANNEL_PUBLIC_RSSHUB
        )
        self.calls.append(channel)
        outcome = self.rsshub.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return list(outcome)  # type: ignore[arg-type]

    def make(self, rsshub: _FakeRSSHub | None) -> FetchOrchestrator:
        return FetchOrchestrator(
            rsshub,
            rss_fetch=self.rss_fetch,
            rsshub_fetch=self.rsshub_fetch,
        )


async def test_rss_direct_success_uses_no_fallback() -> None:
    scripts = _Scripts([[_article("https://x.test/1")]], [])
    orchestrator = scripts.make(_FakeRSSHub())
    result = await orchestrator.fetch_source(_rss_source())
    assert result.ok is True
    assert result.channel == CHANNEL_RSS
    assert len(result.articles) == 1
    assert scripts.calls == [CHANNEL_RSS]


async def test_rss_failure_falls_back_to_local_rsshub() -> None:
    scripts = _Scripts([_FAIL], [[_article("https://x.test/2")]])
    orchestrator = scripts.make(_FakeRSSHub(alive=True))
    source = _rss_source(fallback_rsshub="/bbc/world")
    result = await orchestrator.fetch_source(source)
    assert result.channel == CHANNEL_LOCAL_RSSHUB
    assert result.ok is True
    assert scripts.calls == [CHANNEL_RSS, CHANNEL_LOCAL_RSSHUB]
    assert len(result.errors) == 1  # the failed direct attempt


async def test_rss_failure_chain_reaches_public_rsshub() -> None:
    scripts = _Scripts([_FAIL], [_FAIL, [_article("https://x.test/3")]])
    orchestrator = scripts.make(_FakeRSSHub(alive=True))
    source = _rss_source(fallback_rsshub="/bbc/world")
    result = await orchestrator.fetch_source(source)
    assert result.channel == CHANNEL_PUBLIC_RSSHUB
    assert result.ok is True
    assert scripts.calls == [CHANNEL_RSS, CHANNEL_LOCAL_RSSHUB, CHANNEL_PUBLIC_RSSHUB]
    assert len(result.errors) == 2


async def test_rss_failure_without_fallback_marks_unavailable() -> None:
    scripts = _Scripts([_FAIL], [])
    orchestrator = scripts.make(_FakeRSSHub(alive=True))
    result = await orchestrator.fetch_source(_rss_source(fallback_rsshub=None))
    assert result.ok is False
    assert result.channel is None
    assert result.articles == ()
    assert len(result.errors) == 1
    assert scripts.calls == [CHANNEL_RSS]


async def test_all_channels_failing_marks_unavailable() -> None:
    scripts = _Scripts([_FAIL], [_FAIL, _FAIL])
    orchestrator = scripts.make(_FakeRSSHub(alive=True))
    source = _rss_source(fallback_rsshub="/bbc/world")
    result = await orchestrator.fetch_source(source)
    assert result.ok is False
    assert result.channel is None
    assert len(result.errors) == 3  # rss + local + public


async def test_rsshub_source_local_success() -> None:
    scripts = _Scripts([], [[_article("https://x.test/4")]])
    fake = _FakeRSSHub(alive=True)
    orchestrator = scripts.make(fake)
    result = await orchestrator.fetch_source(_rsshub_source())
    assert result.channel == CHANNEL_LOCAL_RSSHUB
    assert fake.routes == ["/zhihu/hotlist"]
    assert scripts.calls == [CHANNEL_LOCAL_RSSHUB]


async def test_rsshub_source_local_failure_falls_back_to_public() -> None:
    scripts = _Scripts([], [_FAIL, [_article("https://x.test/5")]])
    orchestrator = scripts.make(_FakeRSSHub(alive=True))
    result = await orchestrator.fetch_source(_rsshub_source())
    assert result.channel == CHANNEL_PUBLIC_RSSHUB
    assert scripts.calls == [CHANNEL_LOCAL_RSSHUB, CHANNEL_PUBLIC_RSSHUB]


async def test_rsshub_source_all_channels_fail() -> None:
    scripts = _Scripts([], [_FAIL, _FAIL])
    orchestrator = scripts.make(_FakeRSSHub(alive=True))
    result = await orchestrator.fetch_source(_rsshub_source())
    assert result.ok is False
    assert len(result.errors) == 2


async def test_dead_local_rsshub_is_skipped_straight_to_public() -> None:
    scripts = _Scripts([], [[_article("https://x.test/6")]])
    fake = _FakeRSSHub(alive=False)
    orchestrator = scripts.make(fake)
    result = await orchestrator.fetch_source(_rsshub_source())
    assert result.channel == CHANNEL_PUBLIC_RSSHUB
    assert fake.routes == []           # local never attempted
    assert scripts.calls == [CHANNEL_PUBLIC_RSSHUB]
    assert result.errors == ()


async def test_no_manager_at_all_uses_public_only() -> None:
    scripts = _Scripts([], [[_article("https://x.test/7")]])
    orchestrator = scripts.make(None)
    result = await orchestrator.fetch_source(_rsshub_source())
    assert result.channel == CHANNEL_PUBLIC_RSSHUB
    assert scripts.calls == [CHANNEL_PUBLIC_RSSHUB]


async def test_unknown_source_type_raises() -> None:
    orchestrator = _Scripts([], []).make(None)
    bad = SourceConfig(id="x", name="x", type="carrier-pigeon")
    with pytest.raises(ValueError, match="carrier-pigeon"):
        await orchestrator.fetch_source(bad)


async def test_fetch_all_returns_result_per_source() -> None:
    ok_rss = _rss_source(id="s1")
    dead_rss = _rss_source(id="s2")
    scripts = _Scripts([[_article("https://x.test/8")], _FAIL], [])
    orchestrator = scripts.make(_FakeRSSHub())
    results = await orchestrator.fetch_all([ok_rss, dead_rss])
    assert set(results) == {"s1", "s2"}
    assert results["s1"].ok is True
    assert results["s2"].ok is False


def test_load_sources_parses_repo_config() -> None:
    config = Path(__file__).resolve().parents[1] / "config" / "sources.yaml"
    sources = load_sources(config)
    assert len(sources) >= 10
    by_id = {source.id: source for source in sources}
    assert by_id["zhihu_hotlist"].type == "rsshub"
    assert by_id["zhihu_hotlist"].route == "/zhihu/hotlist"
    assert by_id["bbc_world"].type == "rss"
    assert by_id["bbc_world"].url is not None


def test_load_sources_rejects_invalid_entries(tmp_path: Path) -> None:
    bad = tmp_path / "sources.yaml"
    bad.write_text(
        "sources:\n"
        "  - id: broken\n"
        "    name: Broken\n"
        "    type: rss\n",  # rss without url
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="broken"):
        load_sources(bad)
