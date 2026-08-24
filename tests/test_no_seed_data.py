"""A fresh install must ship no data at all.

The bot's correctness rests on every number tracing back to the real MFL API.
A seeded player, a default scoring weight or a placeholder projection would
break that chain silently -- it would look like real data at the point of use.
So: a migrated database is empty, and the shipped configuration contains no
league data.
"""

from __future__ import annotations

import re
from pathlib import Path

from mflbot.storage.db import Database

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_migrated_database_is_completely_empty(db: Database) -> None:
    counts = db.table_counts()
    assert counts, "migration created no tables"
    non_empty = {table: n for table, n in counts.items() if n}
    assert not non_empty, f"a fresh database shipped rows: {non_empty}"


def test_example_config_contains_no_scoring_or_roster_data() -> None:
    """Behaviour thresholds belong in config; league data does not."""
    text = (REPO_ROOT / "config.example.toml").read_text(encoding="utf-8").lower()
    forbidden = [
        "points_per",
        "passing_yards",
        "rushing_yards",
        "reception =",
        "ppr =",
        "scoring =",
        "roster_size =",
        "trade_deadline =",
        "lineup_slots",
    ]
    present = [token for token in forbidden if token in text]
    assert not present, (
        f"config.example.toml declares league data that must come from the API: "
        f"{present}"
    )


def test_no_module_defines_a_default_scoring_table() -> None:
    """No hardcoded event->points mapping may exist anywhere in the package."""
    suspicious = re.compile(
        r"""(?ix)
        (^|\W)
        (ppr|standard_scoring|default_scoring|SCORING_DEFAULTS)
        \s*[:=]\s*[\{\(]
        """
    )
    offenders = []
    for path in (REPO_ROOT / "src" / "mflbot").rglob("*.py"):
        if suspicious.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, f"hardcoded scoring defaults found in: {offenders}"
