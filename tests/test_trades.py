"""Trade analysis. All inputs are SYNTHETIC."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from mflbot.analysis.trades import (
    build_response_recommendation,
    draft_proposals,
    evaluate_offer,
    position_strengths,
    starters_per_position,
)
from mflbot.analysis.waivers import PlayerValue
from mflbot.config import TradeSettings
from mflbot.domain.models import Player


def value(pid: str, position: str, ros: float, weeks: int = 6) -> PlayerValue:
    return PlayerValue(
        Player(pid, f"Synthetic {pid}", position, "AAA"),
        next_week=ros / max(weeks, 1),
        rest_of_season=ros,
        weeks_covered=weeks,
    )


def with_deadline(settings, days: int = 30):
    return dataclasses.replace(
        settings, trade_deadline=datetime.now(UTC) + timedelta(days=days)
    )


def test_starters_per_position_counts_flex_share(synthetic_settings) -> None:
    counts = starters_per_position(synthetic_settings)
    assert counts["QB"] == 1
    assert counts["RB"] >= 2 and counts["WR"] >= 2


def test_position_strengths_separates_starters_from_depth() -> None:
    values = [value("a", "RB", 100), value("b", "RB", 80), value("c", "RB", 60)]
    strengths = position_strengths(values, {"RB": 2})
    assert strengths["RB"].starter_value == pytest.approx(180)
    assert strengths["RB"].depth_value == pytest.approx(60)


def test_clearly_favourable_offer_is_recommended_for_acceptance() -> None:
    assessment = evaluate_offer(
        "offer-1", "0002",
        we_receive=[value("in", "RB", 120)],
        we_give=[value("out", "RB", 60)],
        our_strengths=position_strengths(
            [value("out", "RB", 60), value("depth", "RB", 50)], {"RB": 1}
        ),
        trade_settings=TradeSettings(),
    )
    assert assessment.verdict == "accept"
    assert assessment.net_value == pytest.approx(60)


def test_clearly_unfavourable_offer_is_recommended_for_rejection() -> None:
    assessment = evaluate_offer(
        "offer-2", "0002",
        we_receive=[value("in", "RB", 30)],
        we_give=[value("out", "RB", 110)],
        our_strengths=position_strengths(
            [value("out", "RB", 110), value("depth", "RB", 90)], {"RB": 1}
        ),
        trade_settings=TradeSettings(),
    )
    assert assessment.verdict == "reject"


def test_roughly_even_offer_yields_counter_not_a_coin_flip() -> None:
    assessment = evaluate_offer(
        "offer-3", "0002",
        we_receive=[value("in", "RB", 100)],
        we_give=[value("out", "RB", 99)],
        our_strengths=position_strengths(
            [value("out", "RB", 99), value("depth", "RB", 90)], {"RB": 1}
        ),
        trade_settings=TradeSettings(),
    )
    assert assessment.verdict == "counter"
    assert "noise" in assessment.reasoning


def test_favourable_value_that_costs_scarce_depth_becomes_a_counter() -> None:
    assessment = evaluate_offer(
        "offer-4", "0002",
        we_receive=[value("in", "WR", 120)],
        we_give=[value("out", "RB", 60)],
        our_strengths=position_strengths([value("out", "RB", 60)], {"RB": 1}),
        trade_settings=TradeSettings(),
    )
    assert assessment.verdict == "counter"
    assert "depth" in assessment.reasoning


def test_unpriced_pieces_lower_confidence_and_are_disclosed() -> None:
    unpriced = PlayerValue(Player("x", "Synthetic X", "RB", "AAA"), None, None, 0)
    assessment = evaluate_offer(
        "offer-5", "0002",
        we_receive=[unpriced],
        we_give=[value("out", "RB", 60)],
        our_strengths={},
        trade_settings=TradeSettings(),
    )
    assert assessment.confidence == "low"
    assert any("No rest-of-season projection" in c for c in assessment.caveats)


def test_counter_verdict_produces_no_approvable_action(synthetic_settings) -> None:
    """There is no single action to approve, and the bot invents no terms."""
    assessment = evaluate_offer(
        "offer-6", "0002",
        we_receive=[value("in", "RB", 100)],
        we_give=[value("out", "RB", 99)],
        our_strengths=position_strengths(
            [value("out", "RB", 99), value("d", "RB", 90)], {"RB": 1}
        ),
        trade_settings=TradeSettings(),
    )
    assert build_response_recommendation(assessment, synthetic_settings, "0001") is None


def test_accept_verdict_produces_an_approvable_action(synthetic_settings) -> None:
    assessment = evaluate_offer(
        "offer-7", "0002",
        we_receive=[value("in", "RB", 120)],
        we_give=[value("out", "RB", 60)],
        our_strengths=position_strengths(
            [value("out", "RB", 60), value("d", "RB", 50)], {"RB": 1}
        ),
        trade_settings=TradeSettings(),
    )
    recommendation = build_response_recommendation(
        assessment, synthetic_settings, "0001"
    )
    assert recommendation is not None
    assert recommendation.payload.accept is True
    assert recommendation.payload.offer_id == "offer-7"


def test_no_proposals_are_drafted_when_the_deadline_is_unknown(
    synthetic_settings,
) -> None:
    ideas, blocks = draft_proposals(
        synthetic_settings, [value("a", "RB", 100)], {}, TradeSettings()
    )
    assert ideas == []
    assert blocks and "deadline is unknown" in blocks[0].reason


def test_no_proposals_are_drafted_after_the_deadline(synthetic_settings) -> None:
    passed = dataclasses.replace(
        synthetic_settings, trade_deadline=datetime.now(UTC) - timedelta(days=1)
    )
    ideas, blocks = draft_proposals(
        passed, [value("a", "RB", 100)], {"0002": [value("b", "WR", 100)]},
        TradeSettings(),
    )
    assert ideas == [] and blocks == []


def test_complementary_imbalance_produces_a_mutually_beneficial_proposal(
    synthetic_settings,
) -> None:
    settings = with_deadline(synthetic_settings)
    # We are deep at RB and thin at WR; they are the reverse.
    ours = [
        value("our-rb1", "RB", 120), value("our-rb2", "RB", 110),
        value("our-rb3", "RB", 100), value("our-wr1", "WR", 40),
    ]
    theirs = [
        value("their-wr1", "WR", 120), value("their-wr2", "WR", 110),
        value("their-wr3", "WR", 100), value("their-rb1", "RB", 40),
    ]
    ideas, blocks = draft_proposals(
        settings, ours, {"0002": theirs}, TradeSettings()
    )
    assert not blocks
    assert ideas, "a clear complementary imbalance should produce a proposal"
    idea = ideas[0]
    assert idea.our_gain > 0 and idea.their_gain > 0
    assert idea.pitch


def test_proposal_count_is_capped(synthetic_settings) -> None:
    settings = with_deadline(synthetic_settings)
    ours = [value(f"our-rb{i}", "RB", 120 - i) for i in range(4)] + [value("our-wr", "WR", 20)]
    others = {
        f"000{i}": [value(f"t{i}-wr{j}", "WR", 120 - j) for j in range(4)]
                   + [value(f"t{i}-rb", "RB", 20)]
        for i in range(2, 8)
    }
    ideas, _ = draft_proposals(
        settings, ours, others, TradeSettings(max_proposals_per_week=2)
    )
    assert len(ideas) <= 2


def test_the_user_is_never_offered_a_trade_with_themselves(synthetic_settings) -> None:
    settings = with_deadline(synthetic_settings)
    ours = [value("our-rb1", "RB", 120), value("our-rb2", "RB", 110),
            value("our-rb3", "RB", 100), value("our-wr1", "WR", 40)]
    ideas, _ = draft_proposals(settings, ours, {"0001": ours}, TradeSettings())
    assert all(idea.to_franchise_id != "0001" for idea in ideas)
