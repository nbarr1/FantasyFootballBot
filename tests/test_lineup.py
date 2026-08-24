"""Lineup optimisation.

All players, positions and projections here are SYNTHETIC.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mflbot.analysis.lineup import (
    Candidate,
    _is_laminar,
    assess_risks,
    diff_lineup,
    optimise_lineup,
    solve_greedy,
)
from mflbot.domain.models import LineupSlot, Player
from mflbot.errors import Missing


def candidate(pid: str, position: str, projection: float, **kw) -> Candidate:
    return Candidate(Player(pid, f"Synthetic {pid}", position, kw.pop("team", "AAA")),
                     projection, **kw)


@pytest.fixture
def pool() -> list[Candidate]:
    return [
        candidate("p-qb1", "QB", 22.0),
        candidate("p-qb2", "QB", 14.0),
        candidate("p-rb1", "RB", 18.0),
        candidate("p-rb2", "RB", 12.0),
        candidate("p-rb3", "RB", 9.0),
        candidate("p-wr1", "WR", 16.0),
        candidate("p-wr2", "WR", 13.0),
        candidate("p-wr3", "WR", 11.0),
        candidate("p-te1", "TE", 8.0),
        candidate("p-te2", "TE", 4.0),
    ]


def test_typical_flex_structure_is_laminar(synthetic_slots) -> None:
    assert _is_laminar(synthetic_slots)


def test_both_solvers_agree_on_a_laminar_structure(synthetic_slots, pool) -> None:
    ilp = optimise_lineup(synthetic_slots, pool, prefer_ilp=True)
    greedy = optimise_lineup(synthetic_slots, pool, prefer_ilp=False)
    assert not isinstance(ilp, Missing) and not isinstance(greedy, Missing)
    assert ilp.total_projection == pytest.approx(greedy.total_projection)


def test_optimal_lineup_selects_the_best_legal_combination(synthetic_slots, pool) -> None:
    solution = optimise_lineup(synthetic_slots, pool)
    # QB 22 + RB 18,12 + WR 16,13 + TE 8 + flex (best remaining of RB9/WR11/TE4) = 100
    assert solution.total_projection == pytest.approx(100.0)
    assert solution.starter_ids == {
        "p-qb1", "p-rb1", "p-rb2", "p-wr1", "p-wr2", "p-te1", "p-wr3"
    }


def test_no_player_occupies_two_seats(synthetic_slots, pool) -> None:
    solution = optimise_lineup(synthetic_slots, pool)
    ids = [a.candidate.player_id for a in solution.assignments]
    assert len(ids) == len(set(ids))


def test_bye_week_players_are_never_started(synthetic_slots) -> None:
    pool = [
        candidate("p-qb1", "QB", 30.0, on_bye=True),
        candidate("p-qb2", "QB", 10.0),
    ]
    slots = (LineupSlot(0, "QB", ("QB",), 1, 1),)
    solution = optimise_lineup(slots, pool)
    assert solution.starter_ids == {"p-qb2"}


def test_greedy_refuses_a_non_laminar_structure_rather_than_guessing() -> None:
    slots = (
        LineupSlot(0, "QB/RB", ("QB", "RB"), 1, 1),
        LineupSlot(1, "RB/WR", ("RB", "WR"), 1, 1),
    )
    result = solve_greedy(slots, [candidate("p-rb1", "RB", 10.0)])
    assert isinstance(result, Missing)
    assert "ILP" in str(result) or "solver" in str(result)


def test_ilp_solves_a_non_laminar_structure() -> None:
    pytest.importorskip("pulp")
    slots = (
        LineupSlot(0, "QB/RB", ("QB", "RB"), 1, 1),
        LineupSlot(1, "RB/WR", ("RB", "WR"), 1, 1),
    )
    pool = [
        candidate("p-qb1", "QB", 20.0),
        candidate("p-rb1", "RB", 15.0),
        candidate("p-wr1", "WR", 5.0),
    ]
    solution = optimise_lineup(slots, pool, prefer_ilp=True)
    assert solution.total_projection == pytest.approx(35.0)


def test_missing_lineup_structure_blocks_rather_than_defaulting() -> None:
    result = optimise_lineup((), [candidate("p-qb1", "QB", 20.0)])
    assert isinstance(result, Missing)
    assert "unknown" in result.reason


def test_out_designation_is_flagged_as_an_urgent_risk() -> None:
    risks = assess_risks(candidate("p-rb1", "RB", 12.0, injury_status="OUT"), None, 3.0)
    assert any(r.startswith("OUT") for r in risks)


def test_kickoff_after_lock_is_flagged() -> None:
    lock = datetime.now(UTC)
    late = candidate("p-rb1", "RB", 12.0, kickoff=lock + timedelta(hours=8))
    risks = assess_risks(late, lock, 3.0)
    assert any("after the lineup locks" in r for r in risks)


def test_kickoff_before_lock_is_not_flagged() -> None:
    lock = datetime.now(UTC) + timedelta(hours=8)
    early = candidate("p-rb1", "RB", 12.0, kickoff=lock - timedelta(hours=2))
    assert not any("after the lineup locks" in r for r in assess_risks(early, lock, 3.0))


def test_diff_reports_only_what_changes(synthetic_slots, pool) -> None:
    solution = optimise_lineup(synthetic_slots, pool)
    submitted = set(solution.starter_ids)
    lookup = {c.player_id: c.player for c in pool}

    assert diff_lineup(solution, submitted, lookup) == []

    submitted.discard("p-wr3")
    submitted.add("p-te2")
    changes = diff_lineup(solution, submitted, lookup)
    assert len(changes) == 1
    assert changes[0].player_in.player_id == "p-wr3"
