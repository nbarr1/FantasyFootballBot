"""Ingestion parsers and change detection. All payloads are SYNTHETIC."""

from __future__ import annotations

import pytest

from mflbot.domain.models import Player
from mflbot.errors import ParseError
from mflbot.ingest.league_state import (
    parse_free_agents,
    parse_rosters,
    parse_transactions,
)
from mflbot.ingest.news.sleeper import PlayerCrosswalk, normalise_name
from mflbot.ingest.players import parse_players
from mflbot.ingest.scores import parse_player_scores, parse_projected_scores


def test_players_parse_and_skip_incomplete_records() -> None:
    payload = {
        "players": {
            "player": [
                {"id": "p1", "name": "Synthetic One", "position": "RB", "team": "AAA"},
                {"id": "p2"},  # no name -- unusable, must be skipped not defaulted
                {"name": "No Id"},
            ]
        }
    }
    players = parse_players(payload)
    assert [p.player_id for p in players] == ["p1"]


def test_single_element_collections_are_normalised() -> None:
    """MFL collapses one-item lists to a bare object across every endpoint."""
    entries = parse_rosters(
        {"rosters": {"franchise": {"id": "0001", "player": {"id": "p1", "status": "ROSTER"}}}}
    )
    assert len(entries) == 1 and entries[0].franchise_id == "0001"


def test_taxi_and_ir_players_are_not_active_roster() -> None:
    entries = parse_rosters(
        {
            "rosters": {
                "franchise": {
                    "id": "0001",
                    "player": [
                        {"id": "p1", "status": "ROSTER"},
                        {"id": "p2", "status": "TAXI_SQUAD"},
                        {"id": "p3", "status": "INJURED_RESERVE"},
                    ],
                }
            }
        }
    )
    active = [e.player_id for e in entries if e.is_active_roster]
    assert active == ["p1"]


def test_malformed_envelope_raises_rather_than_returning_empty() -> None:
    """An empty roster and an unreadable response are different facts."""
    with pytest.raises(ParseError):
        parse_rosters({"unexpected": {}})
    with pytest.raises(ParseError):
        parse_free_agents({"unexpected": {}})


def test_transaction_identity_distinguishes_distinct_events() -> None:
    payload = {
        "transactions": {
            "transaction": [
                {"timestamp": "1700000000", "type": "TRADE", "franchise": "0001",
                 "transaction": "a|b"},
                {"timestamp": "1700000000", "type": "TRADE", "franchise": "0002",
                 "transaction": "c|d"},
            ]
        }
    }
    transactions = parse_transactions(payload)
    assert len({t.transaction_id for t in transactions}) == 2


def test_new_transactions_are_reported_once_only(repos) -> None:
    payload = {
        "transactions": {
            "transaction": [
                {"timestamp": "1700000000", "type": "WAIVER", "franchise": "0001",
                 "transaction": "x"}
            ]
        }
    }
    transactions = parse_transactions(payload)
    first = repos.save_transactions("TEST0001", 2026, transactions)
    second = repos.save_transactions("TEST0001", 2026, transactions)
    assert len(first) == 1
    assert second == [], "an already-seen transaction must not re-trigger analysis"


def test_scores_and_projections_skip_unparseable_values() -> None:
    scores = parse_player_scores(
        {"playerScores": {"playerScore": [
            {"id": "p1", "score": "12.4"},
            {"id": "p2", "score": ""},
            {"id": "p3", "score": "n/a"},
        ]}}
    )
    assert scores == [("p1", 12.4)]

    projections = parse_projected_scores(
        {"projectedScores": {"playerScore": [{"id": "p1", "score": "9.5"}]}}, week=3
    )
    assert projections[0].points == pytest.approx(9.5)
    assert projections[0].week == 3


def test_crosswalk_requires_corroboration_for_an_ambiguous_name() -> None:
    crosswalk = PlayerCrosswalk(
        [Player("1", "Synthetic Twin", "WR", "AAA"), Player("2", "Synthetic Twin", "DT", "BBB")]
    )
    assert crosswalk.resolve("Synthetic Twin") is None
    assert crosswalk.resolve("Synthetic Twin", "WR", "AAA") == "1"


def test_crosswalk_folds_provider_name_spelling_differences() -> None:
    assert normalise_name("D'Andre Synthetic Jr.") == normalise_name("DAndre Synthetic")
    assert normalise_name("Amon-Ra Synthetic") == normalise_name("Amon Ra Synthetic")
    assert normalise_name("José Synthetic") == normalise_name("Jose Synthetic")


def test_unmatched_external_player_is_dropped_not_guessed() -> None:
    crosswalk = PlayerCrosswalk([Player("1", "Synthetic One", "RB", "AAA")])
    assert crosswalk.resolve("Someone Not In This League") is None


def test_news_deduplicates_but_a_changed_status_is_new(repos) -> None:
    from datetime import UTC, datetime

    from mflbot.domain.models import NewsItem

    def item(status: str) -> NewsItem:
        return NewsItem(
            source="mfl_injuries",
            external_id=f"5:p1:{status}",
            player_id="p1",
            player_name=None,
            published_at=datetime.now(UTC),
            classification="injury",
            headline=f"Injury designation: {status}",
        )

    assert len(repos.save_news([item("QUESTIONABLE")])) == 1
    assert repos.save_news([item("QUESTIONABLE")]) == []
    assert len(repos.save_news([item("OUT")])) == 1
