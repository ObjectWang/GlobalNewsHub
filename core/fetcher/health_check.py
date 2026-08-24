"""Source health tracking (PRD task P2.4).

Implements section 4.4:

- Every fetch outcome is appended to a per-source in-memory ring buffer
  (recent_window, settings health.recent_window = 5).
- HEALTH_FAIL_THRESHOLD (3) consecutive failures set
  source_health.disabled = 1 in the database; the scheduler must skip
  disabled sources (via enabled_sources) and the UI shows a warning
  badge from snapshot().
- Users can manually re-enable a source, which resets the consecutive
  failure counter (re_enable).

State is persisted in the source_health table (section 2.3) so
auto-disabling survives restarts; only the ring buffer is memory-only.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from core.constants import HEALTH_FAIL_THRESHOLD
from core.fetcher.fetch_orchestrator import FetchResult, SourceConfig
from core.storage.database import Database

logger = logging.getLogger(__name__)

DEFAULT_RECENT_WINDOW: int = 5  # settings.yaml health.recent_window

_UPSERT_SQL = (
    "INSERT INTO source_health (source_id, name, last_status, fail_count,"
    " last_fetch_at, disabled)"
    " VALUES (?, ?, ?, ?, ?, ?)"
    " ON CONFLICT(source_id) DO UPDATE SET"
    " name=excluded.name, last_status=excluded.last_status,"
    " fail_count=excluded.fail_count, last_fetch_at=excluded.last_fetch_at,"
    " disabled=excluded.disabled"
)


@dataclass(frozen=True)
class SourceHealthStatus:
    """Per-source health view consumed by the UI badge (Phase 4)."""

    source_id: str
    name: str
    last_status: str | None
    fail_count: int
    last_fetch_at: str | None
    disabled: bool
    recent: tuple[bool, ...]  # ring buffer contents, oldest -> newest


class SourceHealthTracker:
    """Records fetch outcomes and auto-disables failing sources."""

    def __init__(
        self,
        db: Database,
        *,
        fail_threshold: int = HEALTH_FAIL_THRESHOLD,
        window: int = DEFAULT_RECENT_WINDOW,
    ) -> None:
        """Bind to the database; ring buffers start empty per instance."""
        self._db = db
        self._fail_threshold = fail_threshold
        self._recent: dict[str, deque[bool]] = {}
        self._window = window

    # -- recording -----------------------------------------------------------

    def record_success(self, source_id: str, name: str, *, article_count: int = 0) -> None:
        """Record a successful fetch; resets the consecutive-failure counter."""
        self._append_recent(source_id, True)
        status = f"ok ({article_count} articles)"
        self._save(source_id, name, status, fail_count=0, disabled=False)
        logger.info("Source %s fetched %d articles", source_id, article_count)

    def record_failure(self, source_id: str, name: str, *, error: str = "") -> None:
        """Record a failed fetch; auto-disable at the failure threshold."""
        self._append_recent(source_id, False)
        fail_count = self._load_fail_count(source_id) + 1
        disabled = fail_count >= self._fail_threshold
        status = f"error: {error}" if error else "error"
        self._save(source_id, name, status, fail_count=fail_count, disabled=disabled)
        if disabled and fail_count == self._fail_threshold:
            logger.warning(
                "Source %s auto-disabled after %d consecutive failures (section 4.4)",
                source_id, fail_count,
            )
        else:
            logger.warning("Source %s fetch failed (%d in a row): %s",
                           source_id, fail_count, error)

    def record_result(self, source: SourceConfig, result: FetchResult) -> None:
        """Convenience bridge: route a FetchResult into record_* calls."""
        if result.ok:
            self.record_success(source.id, source.name, article_count=len(result.articles))
            return
        self.record_failure(
            source.id, source.name, error="; ".join(result.errors) if result.errors else "unknown"
        )

    # -- queries ---------------------------------------------------------------

    def is_disabled(self, source_id: str) -> bool:
        """True when the source_health row is marked disabled."""
        with self._db.session() as conn:
            row = conn.execute(
                "SELECT disabled FROM source_health WHERE source_id = ?", (source_id,)
            ).fetchone()
        return bool(row["disabled"]) if row is not None else False

    def enabled_sources(self, sources: Sequence[SourceConfig]) -> list[SourceConfig]:
        """Filter to sources enabled both in config and in health state."""
        return [
            source for source in sources
            if source.enabled and not self.is_disabled(source.id)
        ]

    def recent_statuses(self, source_id: str) -> tuple[bool, ...]:
        """Ring buffer contents (oldest -> newest); empty for unseen sources."""
        buffer = self._recent.get(source_id)
        return tuple(buffer) if buffer is not None else ()

    def snapshot(self) -> dict[str, SourceHealthStatus]:
        """Every persisted source_health row merged with its ring buffer."""
        with self._db.session() as conn:
            rows = conn.execute(
                "SELECT source_id, name, last_status, fail_count, last_fetch_at,"
                " disabled FROM source_health ORDER BY source_id"
            ).fetchall()
        result: dict[str, SourceHealthStatus] = {}
        for row in rows:
            source_id = str(row["source_id"])
            result[source_id] = SourceHealthStatus(
                source_id=source_id,
                name=str(row["name"]),
                last_status=row["last_status"],
                fail_count=int(row["fail_count"]),
                last_fetch_at=row["last_fetch_at"],
                disabled=bool(row["disabled"]),
                recent=self.recent_statuses(source_id),
            )
        return result

    # -- user actions ---------------------------------------------------------

    def re_enable(self, source_id: str, name: str) -> None:
        """Manually re-enable a source; resets the consecutive-failure counter."""
        self._save(source_id, name, "re-enabled by user", fail_count=0, disabled=False)
        logger.info("Source %s re-enabled by user", source_id)

    # -- internals ---------------------------------------------------------------

    def _append_recent(self, source_id: str, ok: bool) -> None:
        buffer = self._recent.setdefault(source_id, deque(maxlen=self._window))
        buffer.append(ok)

    def _load_fail_count(self, source_id: str) -> int:
        with self._db.session() as conn:
            row = conn.execute(
                "SELECT fail_count FROM source_health WHERE source_id = ?", (source_id,)
            ).fetchone()
        return int(row["fail_count"]) if row is not None else 0

    def _save(
        self,
        source_id: str,
        name: str,
        status: str,
        *,
        fail_count: int,
        disabled: bool,
    ) -> None:
        fetched_at = datetime.now(UTC).isoformat()
        with self._db.session() as conn:
            conn.execute(
                _UPSERT_SQL,
                (source_id, name, status, fail_count, fetched_at, int(disabled)),
            )
