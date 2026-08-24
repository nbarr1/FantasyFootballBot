"""Waiver and add/drop analysis. All inputs are SYNTHETIC."""

from __future__ import annotations

import dataclasses

import pytest
from helpers import FakeProjections

from mflbot.analysis.waivers import (
    analyse_waivers,
    build_recommendations,
    recommend_bid,
    value_players,
)
from mflbot.config import WaiverSettings
from mflbot.domain.models import Franchise, Player, WaiverSystem


@pytest.fixture
def roster() -> list[Player]:
    return [
        Player("p-rb1", "Synthetic RB One", "RB", "AAA"),
        Player("p-rb2", "Synthetic RB Two", "RB", "BBB"),
    ]


@pytest.fixture
def free_agents() -> list[Player]:
    return [
        Player("p-rb9", "Synthetic RB Nine", "RB", "CCC"),
        Player("p-rb8", "Synthetic RB Eight", "RB", "DDD"),
    ]


@pytest.fixture
def projections() -> FakeProjections:
    return FakeProjections(
        {
            5: {"p-rb1": 14.0, "p-rb2": 5.0, "p-rb9": 12.0, "p-rb8": 1.0},
            6: {"p-rb1": 14.0, "p-rb2": 5.0, "p-rb9": 12.0, "p-rb8": 1.0},
        }
    )


def fcfs(settings):
    return dataclasses.replace(
        settings, waiver_system=WaiverSystem.FCFS, waiver_type_raw="FCFS"
    )


def test_surfaces_an_upgrade_over_the_weakest_player_at_the_position(
    synthetic_settings, roster, free_agents, projections
) -> None:
    ideas, blocks = analyse_waivers(
        fcfs(synthetic_settings), roster, free_agents, projections, 5, [5, 6],
        WaiverSettings(),
    )
    assert not blocks
    assert len(ideas) == 1
    assert ideas[0].add.player.player_id == "p-rb9"
    assert ideas[0].drop.player.player_id == "p-rb2"
    assert ideas[0].next_week_delta == pytest.approx(7.0)


def test_respects_the_minimum_delta_threshold(
    synthetic_settings, roster, free_agents, projections
) -> None:
    strict = WaiverSettings(min_projection_delta=20.0, min_ros_delta=100.0)
    ideas, _ = analyse_waivers(
        fcfs(synthetic_settings), roster, free_agents, projections, 5, [5, 6], strict
    )
    assert ideas == []


def test_candidates_below_the_projection_floor_are_ignored(
    synthetic_settings, roster, free_agents, projections
) -> None:
    ideas, _ = analyse_waivers(
        fcfs(synthetic_settings), roster, free_agents, projections, 5, [5, 6],
        WaiverSettings(),
    )
    assert all(idea.add.player.player_id != "p-rb8" for idea in ideas)


def test_max_recommendations_is_honoured(synthetic_settings, projections) -> None:
    roster = [Player(f"p-r{i}", f"Synthetic Roster {i}", "RB", "AAA") for i in range(3)]
    free_agents = [Player(f"p-f{i}", f"Synthetic FA {i}", "RB", "BBB") for i in range(6)]
    by_week = {
        5: {**{p.player_id: 2.0 for p in roster}, **{p.player_id: 20.0 for p in free_agents}}
    }
    ideas, _ = analyse_waivers(
        fcfs(synthetic_settings), roster, free_agents, FakeProjections(by_week), 5, [5],
        WaiverSettings(max_recommendations=2),
    )
    assert len(ideas) == 2


def test_unknown_waiver_system_blocks_rather_than_picking_one(
    synthetic_settings, roster, free_agents, projections
) -> None:
    unknown = dataclasses.replace(
        synthetic_settings, waiver_system=WaiverSystem.UNKNOWN, waiver_type_raw=None
    )
    ideas, blocks = analyse_waivers(
        unknown, roster, free_agents, projections, 5, [5, 6], WaiverSettings()
    )
    assert ideas == []
    assert blocks and blocks[0].feature == "waivers"


def test_blind_bid_without_a_known_budget_blocks(
    synthetic_settings, roster, free_agents, projections
) -> None:
    no_budget = dataclasses.replace(
        synthetic_settings,
        franchises=(Franchise("0001", "Ours", is_owner=True, bbid_budget=None),),
    )
    ideas, blocks = analyse_waivers(
        no_budget, roster, free_agents, projections, 5, [5, 6], WaiverSettings()
    )
    assert ideas == []
    assert blocks and "budget" in blocks[0].describe()


def test_blind_bid_sizes_from_the_real_remaining_budget(synthetic_settings) -> None:
    amount, rationale = recommend_bid(synthetic_settings, 0, 1, 0.15)
    assert amount == pytest.approx(15.0)
    assert "$100" in rationale


def test_bid_never_exceeds_the_remaining_budget(synthetic_settings) -> None:
    broke = dataclasses.replace(
        synthetic_settings,
        franchises=(Franchise("0001", "Ours", is_owner=True, bbid_budget=3.0),),
    )
    amount, _ = recommend_bid(broke, 0, 1, 1.0)
    assert 0 < amount <= 3.0


def test_zero_budget_yields_a_zero_bid_with_an_explanation(synthetic_settings) -> None:
    empty = dataclasses.replace(
        synthetic_settings,
        franchises=(Franchise("0001", "Ours", is_owner=True, bbid_budget=0.0),),
    )
    amount, rationale = recommend_bid(empty, 0, 1, 0.5)
    assert amount == 0.0 and "no bid is possible" in rationale


def test_players_without_projections_are_never_dropped(
    synthetic_settings, projections
) -> None:
    """Dropping an unknown quantity is the user's call, not the bot's."""
    roster = [
        Player("p-rb1", "Synthetic RB One", "RB", "AAA"),
        Player("p-unknown", "Synthetic Unprojected", "RB", "BBB"),
    ]
    free_agents = [Player("p-rb9", "Synthetic RB Nine", "RB", "CCC")]
    by_week = {5: {"p-rb1": 3.0, "p-rb9": 20.0}}  # p-unknown deliberately absent
    ideas, _ = analyse_waivers(
        fcfs(synthetic_settings), roster, free_agents, FakeProjections(by_week), 5, [5],
        WaiverSettings(),
    )
    assert ideas
    assert all(idea.drop.player.player_id != "p-unknown" for idea in ideas)


def test_thin_evidence_lowers_confidence_and_states_why(
    synthetic_settings, roster, free_agents, projections
) -> None:
    ideas, _ = analyse_waivers(
        fcfs(synthetic_settings), roster, free_agents, projections, 5, [5, 6],
        WaiverSettings(),
    )
    idea = ideas[0]
    assert idea.confidence in {"low", "medium"}
    assert any("No news" in c for c in idea.caveats)


def test_recommendation_payload_carries_the_bid_in_a_bidding_league(
    synthetic_settings, roster, free_agents, projections
) -> None:
    ideas, blocks = analyse_waivers(
        synthetic_settings, roster, free_agents, projections, 5, [5, 6], WaiverSettings()
    )
    assert not blocks
    recommendations = build_recommendations(ideas, synthetic_settings, "0001")
    assert recommendations[0].payload.bid_amount is not None
    assert "Projections are estimates" in recommendations[0].rationale


def test_value_players_records_missing_projections_as_missing(roster) -> None:
    values = value_players(roster, FakeProjections({}), 5, [5])
    assert all(v.next_week is None and v.rest_of_season is None for v in values)
    assert all(not v.has_value for v in values)
