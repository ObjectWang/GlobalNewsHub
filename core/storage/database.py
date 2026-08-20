"""SQLite connection management for GlobalNewsHub.

Implements the storage foundation required by PRD v3.0 section 2.3:
SQLite >= 3.35 with WAL journal mode and FTS5, no ORM. Migration scripts
live in ``core/storage/migrations/`` and are applied in filename order;
they use ``IF NOT EXISTS`` and are safe to re-run on every startup.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

_BUSY_TIMEOUT_SECONDS = 5.0


class Database:
    """Factory and migration runner for SQLite connections.

    Each :meth:`session` call opens a fresh connection, so connections are
    never shared across threads. Create and use a session inside the same
    (worker) thread to keep the UI thread free of IO (PRD section 1.2).
    """

    def __init__(self, db_path: Path | str) -> None:
        """Store the database file path; the file is created on connect."""
        self._db_path = Path(db_path)

    @property
    def path(self) -> Path:
        """Filesystem path of the database file."""
        return self._db_path

    def connect(self) -> sqlite3.Connection:
        """Open a new connection with WAL mode, foreign keys and Row factory."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            conn = sqlite3.connect(self._db_path, timeout=_BUSY_TIMEOUT_SECONDS)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
        except sqlite3.Error:
            logger.exception("Failed to open database at %s", self._db_path)
            raise
        return conn

    @contextmanager
    def session(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection; commit on success, roll back on any error.

        The connection is always closed when the block exits.
        """
        conn = self.connect()
        try:
            yield conn
        except Exception:
            conn.rollback()
            logger.exception("Database session rolled back")
            raise
        else:
            conn.commit()
        finally:
            conn.close()

    def initialize(self) -> None:
        """Apply every migration script in filename order (idempotent)."""
        scripts = sorted(MIGRATIONS_DIR.glob("*.sql"))
        if not scripts:
            raise FileNotFoundError(f"No migration scripts in {MIGRATIONS_DIR}")
        with self.session() as conn:
            for script in scripts:
                logger.info("Applying migration %s", script.name)
                conn.executescript(script.read_text(encoding="utf-8"))
