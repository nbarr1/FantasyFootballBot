"""Database connection and schema management.

SQLite is the default because this bot watches one league on one host and the
whole dataset is small. Everything above this module talks to
:class:`~mflbot.storage.repositories.Repositories`, not to SQL, so swapping in
Postgres means writing one more backend rather than editing analysis code.

One connection is shared by every thread in the process -- the scheduler's
worker pool, the dashboard's request handlers and its job worker -- so every
call into it is serialised here. A multi-statement write goes through
:meth:`Database.transaction`, which holds the lock for the whole block: without
that, one thread's ``commit()`` could land between another thread's statements
and save half of its write.
"""

from __future__ import annotations

import functools
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

from ..errors import ConfigError

T = TypeVar("T")

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

#: How long a statement waits for another *process* holding SQLite's write lock
#: (a CLI command alongside the server, say) before giving up.
BUSY_TIMEOUT_SECONDS = 30.0

#: Columns added after a table first shipped. ``CREATE TABLE IF NOT EXISTS``
#: leaves an existing table alone, so a database created by an earlier version
#: gets these added in place by :meth:`Database.migrate`.
_ADDED_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "approval_tokens": (("revoked_at", "TEXT"), ("revoked_reason", "TEXT")),
}


def utc_now_iso() -> str:
    """Timestamps are stored as ISO-8601 UTC strings, always."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def atomic(method: Callable[..., T]) -> Callable[..., T]:
    """Run a repository method as one :meth:`Database.transaction`.

    For methods on objects that hold the database as ``self.db``.
    """

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self.db.transaction():
            return method(self, *args, **kwargs)

    return wrapper


class Database:
    """Owns the connection, serialises access to it, and applies the schema."""

    def __init__(self, path: Path | str = "data/mflbot.db") -> None:
        self.path = Path(path)
        in_memory = str(self.path) == ":memory:"
        if not in_memory:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._local = threading.local()
        self.connection = sqlite3.connect(
            self.path,
            detect_types=sqlite3.PARSE_DECLTYPES,
            check_same_thread=False,
            timeout=BUSY_TIMEOUT_SECONDS,
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        if not in_memory:
            # WAL lets a CLI command or `bot heartbeat` read while the server
            # writes, instead of the two taking turns on one file lock.
            self.connection.execute("PRAGMA journal_mode = WAL")

    @classmethod
    def from_settings(cls, settings, base_dir: Path | str | None = None) -> Database:
        if settings.engine == "sqlite":
            path = Path(settings.path)
            if base_dir is not None and not path.is_absolute() and str(path) != ":memory:":
                path = Path(base_dir) / path
            return cls(path)
        raise ConfigError(
            f"Storage engine '{settings.engine}' is not implemented. "
            "SQLite is the shipped backend; see \"Storage\" in the README for what "
            "swapping in another one involves."
        )

    def migrate(self) -> None:
        """Create any missing tables and columns. Safe to call on every startup.

        This never inserts a row. A freshly migrated database is empty, and
        that is the correct state until ingestion runs.
        """
        with self._lock:
            self.connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
            for table, columns in _ADDED_COLUMNS.items():
                existing = {
                    row["name"]
                    for row in self.connection.execute(f"PRAGMA table_info({table})")
                }
                for name, declaration in columns:
                    if name not in existing:
                        self.connection.execute(
                            f"ALTER TABLE {table} ADD COLUMN {name} {declaration}"
                        )
            self.connection.commit()

    def close(self) -> None:
        with self._lock:
            self.connection.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- transactions ------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Run a block of statements as one atomic write.

        Holds the connection's lock for the whole block, so no other thread's
        statements or commits can interleave with it. Commits on success and
        rolls back on any exception. Nested blocks join the outermost one.
        """
        with self._lock:
            depth = getattr(self._local, "depth", 0)
            self._local.depth = depth + 1
            try:
                yield
            except BaseException:
                if depth == 0:
                    self.connection.rollback()
                raise
            else:
                if depth == 0:
                    self.connection.commit()
            finally:
                self._local.depth = depth

    # -- small helpers used by the repositories ---------------------------

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.connection.execute(sql, params)

    def executemany(self, sql: str, seq) -> sqlite3.Cursor:
        with self._lock:
            return self.connection.executemany(sql, seq)

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self._lock:
            cursor = self.connection.execute(sql, params)
            try:
                return cursor.fetchall()
            finally:
                cursor.close()

    def query_one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        with self._lock:
            cursor = self.connection.execute(sql, params)
            try:
                return cursor.fetchone()
            finally:
                cursor.close()

    def commit(self) -> None:
        """Commit, unless inside :meth:`transaction`, which commits on exit."""
        with self._lock:
            if getattr(self._local, "depth", 0) == 0:
                self.connection.commit()

    def table_counts(self) -> dict[str, int]:
        """Row count per table -- used by `bot status` and by the test that
        asserts a fresh install ships no data."""
        tables = [
            row["name"]
            for row in self.query(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {
            table: self.query_one(f"SELECT COUNT(*) AS n FROM {table}")["n"]  # noqa: S608
            for table in sorted(tables)
        }
