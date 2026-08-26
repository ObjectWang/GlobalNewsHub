"""Periodic auto-refresh scheduling (PRD section 1.2, APScheduler >= 3.10).

The scheduler runs in its own daemon thread and fires a *callback*; the
callback must be thread-safe. In ``main.py`` the callback emits a Qt
signal on a bridge object living in the main thread, which turns timer
ticks into queued slot calls — Qt objects are never touched from the
APScheduler thread.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler

logger = logging.getLogger(__name__)

_JOB_ID = "globalnewshub_auto_refresh"


class RefreshScheduler:
    """Thin, testable wrapper around APScheduler's background scheduler."""

    def __init__(self, refresh_callback: Callable[[], None], interval_minutes: int = 30) -> None:
        """Store the tick callback and interval (no threads started yet)."""
        self._callback = refresh_callback
        self._interval_minutes = max(1, int(interval_minutes))
        self._scheduler: BackgroundScheduler | None = None

    def start(self) -> None:
        """Start emitting ticks every ``interval_minutes`` (idempotent)."""
        if self._scheduler is not None:
            return
        scheduler = BackgroundScheduler(daemon=True)
        scheduler.add_job(
            self._callback,
            trigger="interval",
            minutes=self._interval_minutes,
            id=_JOB_ID,
            coalesce=True,
            max_instances=1,
        )
        scheduler.start()
        self._scheduler = scheduler
        logger.info("Auto-refresh scheduled every %d minute(s)", self._interval_minutes)

    def stop(self) -> None:
        """Shut the scheduler down; safe to call repeatedly."""
        if self._scheduler is None:
            return
        self._scheduler.shutdown(wait=False)
        self._scheduler = None
        logger.info("Auto-refresh stopped")

    def jobs(self) -> list[Any]:
        """Registered APScheduler jobs (empty when not started)."""
        if self._scheduler is None:
            return []
        jobs: list[Any] = list(self._scheduler.get_jobs())
        return jobs

    def run_once(self) -> None:
        """Invoke the callback synchronously (tests / manual refresh)."""
        self._callback()
