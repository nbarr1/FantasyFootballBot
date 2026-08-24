"""Shared fixtures.

Every fixture in this file builds SYNTHETIC data, clearly labelled as such, for
the sole purpose of exercising logic. None of it is a default, a seed, or a
fallback: the application itself ships with empty tables, and
``test_no_seed_data.py`` asserts that.
"""

from __future__ import annotations

import os

import pytest

from mflbot.approval.token import TokenService
from mflbot.domain.models import Franchise, LeagueSettings, LineupSlot, Player, WaiverSystem
from mflbot.recommend.store import RecommendationStore
from mflbot.storage.db import Database
from mflbot.storage.repositories import Repositories

os.environ.setdefault("MFLBOT_APPROVAL_SECRET", "test-only-signing-key")


@pytest.fixture
def db() -> Database:
    database = Database(":memory:")
    database.migrate()
    yield database
    database.close()


@pytest.fixture
def repos(db: Database) -> Repositories:
    return Repositories(db)


@pytest.fixture
def store(db: Database) -> RecommendationStore:
    return RecommendationStore(db)


@pytest.fixture
def tokens(db: Database) -> TokenService:
    return TokenService(db=db, secret=b"test-only-signing-key")


@pytest.fixture
def synthetic_slots() -> tuple[LineupSlot, ...]:
    """SYNTHETIC lineup structure: 1 QB, 2 RB, 2 WR, 1 TE, 1 RB/WR/TE flex."""
    return (
        LineupSlot(0, "QB", ("QB",), 1, 1),
        LineupSlot(1, "RB", ("RB",), 2, 2),
        LineupSlot(2, "WR", ("WR",), 2, 2),
        LineupSlot(3, "TE", ("TE",), 1, 1),
        LineupSlot(4, "RB/WR/TE", ("RB", "WR", "TE"), 1, 1),
    )


@pytest.fixture
def synthetic_settings(synthetic_slots) -> LeagueSettings:
    """SYNTHETIC league settings. Not representative of any real league."""
    return LeagueSettings(
        league_id="TEST0001",
        season=2026,
        name="Synthetic Test League",
        franchise_count=2,
        roster_size=16,
        waiver_type_raw="BBID",
        waiver_system=WaiverSystem.BLIND_BID,
        lineup_slots=synthetic_slots,
        franchises=(
            Franchise("0001", "Ours", is_owner=True, bbid_budget=100.0),
            Franchise("0002", "Theirs", bbid_budget=100.0),
        ),
    )


@pytest.fixture
def synthetic_players() -> list[Player]:
    """SYNTHETIC players with placeholder names, never real NFL identities."""
    return [
        Player("p-qb1", "Synthetic QB One", "QB", "AAA"),
        Player("p-qb2", "Synthetic QB Two", "QB", "BBB"),
        Player("p-rb1", "Synthetic RB One", "RB", "AAA"),
        Player("p-rb2", "Synthetic RB Two", "RB", "BBB"),
        Player("p-rb3", "Synthetic RB Three", "RB", "CCC"),
        Player("p-wr1", "Synthetic WR One", "WR", "AAA"),
        Player("p-wr2", "Synthetic WR Two", "WR", "BBB"),
        Player("p-wr3", "Synthetic WR Three", "WR", "CCC"),
        Player("p-te1", "Synthetic TE One", "TE", "AAA"),
        Player("p-te2", "Synthetic TE Two", "TE", "BBB"),
    ]


class FakeProjections:
    """Stands in for ProjectionProvider with SYNTHETIC per-week points."""

    def __init__(self, by_week: dict[int, dict[str, float]]) -> None:
        self._by_week = by_week

    def week(self, week: int) -> dict[str, float]:
        return self._by_week.get(week, {})

    def rest_of_season(self, player_id: str, weeks):
        from mflbot.errors import Missing

        total, covered = 0.0, []
        for week in weeks:
            points = self.week(week).get(player_id)
            if points is None:
                continue
            total += points
            covered.append(week)
        if not covered:
            return Missing("no projections", {"player_id": player_id})
        return round(total, 3), covered

    def coverage(self, player_ids, week: int) -> float:
        if not player_ids:
            return 0.0
        available = self.week(week)
        return sum(1 for pid in player_ids if pid in available) / len(player_ids)
