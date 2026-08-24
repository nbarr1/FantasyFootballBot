"""Database connection and schema management.

SQLite is the default because this bot watches one league on one host and the
whole dataset is small. Everything above this module talks to
:class:`~mflbot.storage.repositories.Repositories`, not to SQL, so swapping in
Postgres means writing one more backend rather than editing analysis code.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from ..errors import ConfigError

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def utc_now_iso() -> str:
    """Timestamps are stored as ISO-8601 UTC strings, always."""
    return datetime.now(UTC).isoformat(timespec="seconds")


class Database:
    """Owns the connection and applies the schema."""

    def __init__(self, path: Path | str = "data/mflbot.db") -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(
            self.path, detect_types=sqlite3.PARSE_DECLTYPES, check_same_thread=False
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")

    @classmethod
    def from_settings(cls, settings) -> Database:
        if settings.engine == "sqlite":
            return cls(settings.path)
        raise ConfigError(
            f"Storage engine '{settings.engine}' is not implemented. "
            "SQLite is the shipped backend; see README for the Postgres swap path."
        )

    def migrate(self) -> None:
        """Create any missing tables. Safe to call on every startup.

        This never inserts a row. A freshly migrated database is empty, and
        that is the correct state until ingestion runs.
        """
        self.connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- small helpers used by the repositories ---------------------------

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        return self.connection.execute(sql, params)

    def executemany(self, sql: str, seq) -> sqlite3.Cursor:
        return self.connection.executemany(sql, seq)

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return self.connection.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        return self.connection.execute(sql, params).fetchone()

    def commit(self) -> None:
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
