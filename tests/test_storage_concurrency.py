"""The one SQLite connection is shared by every thread in the process.

`bot serve --with-scheduler` runs the scheduler's worker pool, the dashboard's
job worker and its request handlers against the same connection, and every
page render writes (it expires stale recommendations). Before access was
serialised, a handful of threads doing ordinary repository work produced
"cannot start a transaction within a transaction", "cannot commit - no
transaction is active" and worse. These tests hold the fix in place.
"""

from __future__ import annotations

import threading

import pytest

from mflbot.domain.models import Player
from mflbot.recommend.store import RecommendationStore
from mflbot.storage.db import Database
from mflbot.storage.repositories import Repositories


@pytest.fixture
def file_db(tmp_path):
    database = Database(tmp_path / "shared.db")
    database.migrate()
    yield database
    database.close()


def test_concurrent_repository_work_on_one_connection_does_not_fail(file_db) -> None:
    repos, store = Repositories(file_db), RecommendationStore(file_db)
    errors: list[str] = []

    def worker(kind: str) -> None:
        for i in range(150):
            try:
                if kind == "state":
                    repos.set_state(f"job:{threading.get_ident()}", str(i))
                elif kind == "expire":
                    store.expire_stale()
                    store.pending()
                elif kind == "players":
                    repos.upsert_players(
                        [Player(f"p{threading.get_ident()}-{j}", "SYNTHETIC", "RB", "AAA")
                         for j in range(10)]
                    )
                else:
                    repos.blocked_features()
                    file_db.table_counts()
            except Exception as exc:  # noqa: BLE001 - collected and asserted on
                errors.append(f"{type(exc).__name__}: {exc}")

    threads = [
        threading.Thread(target=worker, args=(kind,))
        for kind in ("state", "expire", "players", "read") * 3
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert not file_db.connection.in_transaction


def test_a_failed_transaction_leaves_nothing_behind(file_db) -> None:
    repos = Repositories(file_db)
    with pytest.raises(RuntimeError), file_db.transaction():
        repos.set_state("first", "1")
        repos.set_state("second", "2")
        raise RuntimeError("half-way")

    assert repos.get_state("first") is None
    assert repos.get_state("second") is None


def test_an_old_database_gains_the_token_revocation_columns(tmp_path) -> None:
    """A database created before revocation existed is migrated in place."""
    import sqlite3

    path = tmp_path / "old.db"
    legacy = sqlite3.connect(path)
    legacy.execute(
        "CREATE TABLE approval_tokens (token_id TEXT PRIMARY KEY, recommendation_id "
        "TEXT NOT NULL, payload_hash TEXT NOT NULL, signature TEXT NOT NULL, "
        "issued_at TEXT NOT NULL, expires_at TEXT NOT NULL, consumed_at TEXT, "
        "approved_by TEXT NOT NULL)"
    )
    legacy.commit()
    legacy.close()

    database = Database(path)
    database.migrate()
    columns = {r["name"] for r in database.query("PRAGMA table_info(approval_tokens)")}
    database.close()
    assert {"revoked_at", "revoked_reason"} <= columns
