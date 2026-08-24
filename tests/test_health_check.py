"""Tests for core.fetcher.health_check (PRD task P2.4).

Acceptance: a source is auto-disabled after HEALTH_FAIL_THRESHOLD (3)
consecutive failures; a success resets the counter; users can manually
re-enable. State persists in the source_health table (section 2.3) and
the recent-window ring buffer stays in memory (section 4.4).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from core.constants import HEALTH_FAIL_THRESHOLD
from core.fetcher.fetch_orchestrator import FetchResult, SourceConfig
from core.fetcher.health_check import SourceHealthTracker
from core.storage.database import Database


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "health.db")
    database.initialize()
    yield database


@pytest.fixture
def tracker(db: Database) -> SourceHealthTracker:
    return SourceHealthTracker(db, fail_threshold=HEALTH_FAIL_THRESHOLD, window=5)


def _source(source_id: str = "bbc_world", enabled: bool = True) -> SourceConfig:
    return SourceConfig(
        id=source_id, name="BBC World", type="rss",
        url="https://feeds.example.com/world.xml", enabled=enabled,
    )


def test_three_consecutive_failures_auto_disable(tracker: SourceHealthTracker) -> None:
    for _ in range(HEALTH_FAIL_THRESHOLD - 1):
        tracker.record_failure("bbc_world", "BBC World", error="timeout")
        assert tracker.is_disabled("bbc_world") is False
    tracker.record_failure("bbc_world", "BBC World", error="timeout")
    assert tracker.is_disabled("bbc_world") is True


def test_success_resets_consecutive_counter(tracker: SourceHealthTracker) -> None:
    tracker.record_failure("s", "S", error="x")
    tracker.record_failure("s", "S", error="x")
    tracker.record_success("s", "S", article_count=12)
    # Counter restarted: two more failures must not disable.
    tracker.record_failure("s", "S", error="x")
    tracker.record_failure("s", "S", error="x")
    assert tracker.is_disabled("s") is False
    tracker.record_failure("s", "S", error="x")
    assert tracker.is_disabled("s") is True


def test_ring_buffer_keeps_last_window_only(tracker: SourceHealthTracker) -> None:
    outcomes = [True, False, True, False, False, True, False]
    for ok in outcomes:
        if ok:
            tracker.record_success("s", "S")
        else:
            tracker.record_failure("s", "S", error="x")
    assert tracker.recent_statuses("s") == tuple(outcomes[-5:])
    assert tracker.recent_statuses("never-seen") == ()


def test_re_enable_resets_counter_and_flag(tracker: SourceHealthTracker) -> None:
    for _ in range(HEALTH_FAIL_THRESHOLD):
        tracker.record_failure("s", "S", error="x")
    assert tracker.is_disabled("s") is True
    tracker.re_enable("s", "S")
    assert tracker.is_disabled("s") is False
    # After re-enable the full threshold applies again.
    tracker.record_failure("s", "S", error="x")
    tracker.record_failure("s", "S", error="x")
    assert tracker.is_disabled("s") is False


def test_re_enable_of_unknown_source_creates_enabled_row(
    tracker: SourceHealthTracker,
) -> None:
    tracker.re_enable("brand-new", "Brand New")
    assert tracker.is_disabled("brand-new") is False


def test_state_persists_across_instances(db: Database) -> None:
    first = SourceHealthTracker(db, fail_threshold=HEALTH_FAIL_THRESHOLD)
    for _ in range(HEALTH_FAIL_THRESHOLD):
        first.record_failure("s", "S", error="x")
    second = SourceHealthTracker(db, fail_threshold=HEALTH_FAIL_THRESHOLD)
    assert second.is_disabled("s") is True
    second.re_enable("s", "S")
    third = SourceHealthTracker(db, fail_threshold=HEALTH_FAIL_THRESHOLD)
    assert third.is_disabled("s") is False


def test_enabled_sources_filters_disabled_and_config_off(
    tracker: SourceHealthTracker,
) -> None:
    sources = [_source("a"), _source("b"), _source("c", enabled=False)]
    for _ in range(HEALTH_FAIL_THRESHOLD):
        tracker.record_failure("b", "B", error="x")
    kept = tracker.enabled_sources(sources)
    assert [source.id for source in kept] == ["a"]


def test_record_result_maps_fetch_outcome(tracker: SourceHealthTracker) -> None:
    source = _source("a")
    ok_result = FetchResult("a", (), "rss")
    bad_result = FetchResult("a", (), None, errors=("rss: boom",))
    tracker.record_result(source, ok_result)
    assert tracker.is_disabled("a") is False
    for _ in range(HEALTH_FAIL_THRESHOLD):
        tracker.record_result(source, bad_result)
    assert tracker.is_disabled("a") is True
    status = tracker.snapshot()["a"]
    assert "rss: boom" in (status.last_status or "")


def test_snapshot_merges_db_row_and_ring(tracker: SourceHealthTracker) -> None:
    tracker.record_success("a", "A", article_count=3)
    tracker.record_failure("a", "A", error="boom")
    snapshot = tracker.snapshot()
    status = snapshot["a"]
    assert status.name == "A"
    assert status.fail_count == 1
    assert status.disabled is False
    assert status.recent == (True, False)
    assert status.last_fetch_at is not None
    assert status.last_status is not None
