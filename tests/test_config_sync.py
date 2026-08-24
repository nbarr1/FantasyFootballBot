"""League configuration parsing.

Payloads here are SYNTHETIC shapes exercising the probe's branches. The point of
most of these tests is what happens when a field is *absent*: the parser must
record a miss, never invent a value.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from mflbot.domain.models import WaiverSystem
from mflbot.errors import ParseError
from mflbot.ingest.config_sync import (
    FieldProbe,
    evaluate_blocks,
    normalise_waiver_system,
    parse_league_settings,
    parse_lineup_slots,
)
from mflbot.analysis.rules_parser import ParsedRules, ScoringRule, ScoringRuleGap


def test_probe_records_a_miss_instead_of_returning_a_default() -> None:
    probe = FieldProbe({"present": "1"})
    assert probe.text("present", "present") == "1"
    assert probe.integer("absent", "absent", "alsoAbsent") is None
    assert any("absent" in m for m in probe.missed)


def test_probe_reports_a_non_numeric_value_rather_than_coercing_to_zero() -> None:
    probe = FieldProbe({"count": "many"})
    assert probe.integer("count", "count") is None
    assert any("not a number" in m for m in probe.missed)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("BBID", WaiverSystem.BLIND_BID),
        ("Blind Bidding", WaiverSystem.BLIND_BID),
        ("FCFS", WaiverSystem.FCFS),
        ("Reverse Order", WaiverSystem.WAIVER_ORDER),
        ("none", WaiverSystem.NONE),
        ("something new", WaiverSystem.UNKNOWN),
        (None, WaiverSystem.UNKNOWN),
        ("", WaiverSystem.UNKNOWN),
    ],
)
def test_waiver_system_normalisation(raw, expected) -> None:
    assert normalise_waiver_system(raw) == expected


def test_flex_eligibility_comes_from_the_slot_name() -> None:
    slots, misses = parse_lineup_slots(
        {"position": [{"name": "QB", "limit": "1"}, {"name": "RB/WR/TE", "limit": "1-2"}]}
    )
    assert not misses
    assert slots[1].eligible_positions == ("RB", "WR", "TE")
    assert (slots[1].min_starters, slots[1].max_starters) == (1, 2)


def test_unparseable_starters_section_is_reported() -> None:
    slots, misses = parse_lineup_slots(None)
    assert slots == ()
    assert misses


def test_missing_league_object_raises_rather_than_returning_empty_settings() -> None:
    with pytest.raises(ParseError, match="did not contain a 'league' object"):
        parse_league_settings({"something": "else"}, "TEST0001", 2026)


def test_epoch_timestamps_are_parsed_to_utc() -> None:
    payload = {"league": {"name": "X", "tradeDeadline": "1700000000"}}
    settings, _ = parse_league_settings(payload, "TEST0001", 2026)
    assert settings.trade_deadline == datetime.fromtimestamp(1700000000, tz=UTC)


def test_absent_settings_stay_none_and_are_listed_as_missing() -> None:
    payload = {"league": {"name": "Sparse League"}}
    settings, missing = parse_league_settings(payload, "TEST0001", 2026)
    assert settings.roster_size is None
    assert settings.trade_deadline is None
    assert settings.waiver_system == WaiverSystem.UNKNOWN
    assert any("roster_size" in m for m in missing)
    assert any("trade_deadline" in m for m in missing)


def test_owner_franchise_is_marked_only_when_explicitly_identified() -> None:
    payload = {
        "league": {
            "franchises": {
                "count": "2",
                "franchise": [
                    {"id": "0001", "name": "A"},
                    {"id": "0002", "name": "B"},
                ],
            }
        }
    }
    settings, _ = parse_league_settings(payload, "TEST0001", 2026)
    assert settings.owner_franchise is None, "the bot must not guess which team is ours"

    settings, _ = parse_league_settings(payload, "TEST0001", 2026, owner_franchise_id="0002")
    assert settings.owner_franchise.franchise_id == "0002"


def test_unparsed_scoring_rules_block_the_scoring_feature() -> None:
    payload = {"league": {"name": "X", "tradeDeadline": "1700000000",
                          "waiverType": "BBID"}}
    settings, _ = parse_league_settings(payload, "TEST0001", 2026)
    parsed = ParsedRules(
        rules=(),
        gaps=(ScoringRuleGap(0, ("QB",), "XA", "if(x)", None, "unreadable"),),
    )
    blocked = {b.feature for b in evaluate_blocks(settings, parsed)}
    assert "scoring" in blocked


def test_a_fully_configured_league_blocks_nothing(synthetic_slots) -> None:
    payload = {
        "league": {
            "name": "Complete League",
            "waiverType": "FCFS",
            "tradeDeadline": "1700000000",
            "rosterSize": "16",
            "starters": {
                "position": [
                    {"name": "QB", "limit": "1"},
                    {"name": "RB", "limit": "2"},
                ]
            },
            "franchises": {"count": "2", "franchise": [{"id": "0001", "name": "A"}]},
        }
    }
    settings, _ = parse_league_settings(payload, "TEST0001", 2026, owner_franchise_id="0001")
    parsed = ParsedRules(
        rules=(ScoringRule(0, ("QB",), "XA", "*0.1", None, "per_unit", 0.1, None, None),),
        gaps=(),
    )
    assert evaluate_blocks(settings, parsed) == ()


def test_settings_round_trip_through_storage(repos, synthetic_settings) -> None:
    repos.save_league_settings(synthetic_settings, {"league": {"name": "x"}})
    loaded = repos.load_league_settings(
        synthetic_settings.league_id, synthetic_settings.season
    )
    assert loaded is not None
    assert loaded.waiver_system == synthetic_settings.waiver_system
    assert len(loaded.lineup_slots) == len(synthetic_settings.lineup_slots)
    assert loaded.owner_franchise.franchise_id == "0001"
    assert loaded.lineup_slots[4].eligible_positions == ("RB", "WR", "TE")
