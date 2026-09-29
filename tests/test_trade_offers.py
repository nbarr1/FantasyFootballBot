"""Trade offers made to you: parsing pendingTrades, evaluating each offer once,
and checking the outcome of a response by exact id.

Every id, name and number here is SYNTHETIC.
"""

from __future__ import annotations

import pytest
from test_end_to_end import PAYLOADS, WEEK, ingest_everything
from test_end_to_end import context as e2e_context  # noqa: F401 - fixture
from test_executor import FakeReadClient, FakeWriteClient, token_for

from mflbot.errors import ParseError
from mflbot.execute.executor import Executor
from mflbot.ingest.league_state import parse_pending_trades
from mflbot.mfl.endpoints import Capability
from mflbot.recommend.models import (
    Confidence,
    Evidence,
    Recommendation,
    RecommendationKind,
    RecommendationStatus,
    TradeResponsePayload,
)


def pending(*trades):
    return {"pendingTrades": {"pendingTrade": list(trades)}}


def offer(trade_id="t-100", offering="0002", offered_to="0001",
          gives="p-rb3,", receives="p-rb2,", **extra):
    node = {
        "trade_id": trade_id,
        "offeringteam": offering,
        "offeredto": offered_to,
        "will_give_up": gives,
        "will_receive": receives,
    }
    node.update(extra)
    return node


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------

def test_an_offer_is_read_from_the_offering_franchises_side() -> None:
    offers, problems = parse_pending_trades(pending(offer(expires="4102444800")))
    assert problems == []
    [parsed] = offers
    assert parsed.gives == ("p-rb3",) and parsed.receives == ("p-rb2",)
    # From the receiving franchise's side, the two lists swap.
    assert parsed.assets_for("0001") == (("p-rb3",), ("p-rb2",))
    assert parsed.expires is not None


def test_a_partial_offer_is_reported_not_guessed() -> None:
    broken = offer()
    del broken["will_receive"]
    offers, problems = parse_pending_trades(pending(broken))
    assert offers == []
    assert "will_receive" in problems[0]


def test_a_single_offer_need_not_be_wrapped_in_a_list() -> None:
    offers, _ = parse_pending_trades({"pendingTrades": {"pendingTrade": offer()}})
    assert [o.trade_id for o in offers] == ["t-100"]


def test_no_pending_trades_object_is_a_parse_error() -> None:
    with pytest.raises(ParseError):
        parse_pending_trades({"somethingElse": {}})


# ---------------------------------------------------------------------------
# evaluation, end to end against the synthetic league
# ---------------------------------------------------------------------------

@pytest.fixture
def league_with_offers(e2e_context, monkeypatch):  # noqa: F811 - fixture reuse
    def with_offers(*trades):
        monkeypatch.setitem(PAYLOADS, "pendingTrades", pending(*trades))
        e2e_context.client.cache.clear()
        e2e_context.repos.set_state("current_week", str(WEEK))
        return e2e_context

    ingest_everything(e2e_context)
    return with_offers


def test_an_offer_to_you_becomes_one_response_recommendation(league_with_offers) -> None:
    # They offer RB Three (6 projected) for our RB One and WR One (32): a clear loss.
    context = league_with_offers(offer(gives="p-rb3,", receives="p-rb1,p-wr1,"))

    result = context.run_offer_analysis()

    [recommendation] = [
        r for r in context.store.pending() if r.kind == RecommendationKind.TRADE_RESPONSE
    ]
    assert recommendation.payload.offer_id == "t-100"
    assert recommendation.payload.response == "reject"
    assert "1 response recommendation" in result


def test_each_offer_is_evaluated_once(league_with_offers) -> None:
    context = league_with_offers(offer(gives="p-rb3,", receives="p-rb1,p-wr1,"))
    context.run_offer_analysis()
    again = context.run_offer_analysis()
    responses = [r for r in context.store.all() if r.kind == RecommendationKind.TRADE_RESPONSE]
    assert len(responses) == 1
    assert "none new" in again


def test_an_offer_you_made_is_not_answered_by_you(league_with_offers) -> None:
    context = league_with_offers(offer(offering="0001", offered_to="0002"))
    context.run_offer_analysis()
    assert not [r for r in context.store.all() if r.kind == RecommendationKind.TRADE_RESPONSE]


def test_an_offer_with_a_draft_pick_is_left_to_your_judgement(league_with_offers) -> None:
    context = league_with_offers(offer(gives="p-rb3,DP_01_05,", receives="p-rb2,"))
    result = context.run_offer_analysis()
    assert not [r for r in context.store.all() if r.kind == RecommendationKind.TRADE_RESPONSE]
    assert "left to your judgement" in result


# ---------------------------------------------------------------------------
# preconditions and confirmation match ids exactly
# ---------------------------------------------------------------------------

def response_recommendation(store, offer_id="12"):
    recommendation = Recommendation(
        kind=RecommendationKind.TRADE_RESPONSE,
        payload=TradeResponsePayload(
            capability=Capability.RESPOND_TO_TRADE,
            league_id="TEST0001",
            franchise_id="0001",
            offer_id=offer_id,
            response="accept",
        ),
        rationale="synthetic",
        evidence=Evidence(),
        confidence=Confidence.MEDIUM,
        status=RecommendationStatus.APPROVED,
    )
    store.save(recommendation)
    return recommendation


def test_an_offer_id_inside_another_id_does_not_count_as_open(repos, store) -> None:
    recommendation = response_recommendation(store, offer_id="12")
    # Offer 123 is open; offer 12 is not. A text search would find "12".
    read = FakeReadClient(pending_trades=pending(offer(trade_id="123", timestamp="1712")))
    write = FakeWriteClient()
    outcome = Executor(write, read, repos, store).execute(
        recommendation, token_for(recommendation)
    )
    assert write.calls == []
    assert "no longer pending" in outcome.message


def test_a_lineup_is_confirmed_only_when_mfl_lists_those_starters(repos, store) -> None:
    from mflbot.recommend.models import LineupPayload

    payload = LineupPayload(
        capability=Capability.SUBMIT_LINEUP, league_id="TEST0001", franchise_id="0001",
        week=5, starter_ids=("p-qb1", "p-rb1"),
    )
    executor = Executor(FakeWriteClient(), None, repos, store)

    def results(*starters):
        players = [{"id": pid, "status": "starter"} for pid in starters]
        return {"weeklyResults": {"matchup": {"franchise": [
            {"id": "0001", "player": players},
            {"id": "0002", "player": [{"id": "p-qb2", "status": "starter"}]},
        ]}}}

    executor._read = FakeReadClient({"weeklyResults": results("p-qb1", "p-rb1")})
    assert executor.confirm(payload)[0] is True

    executor._read = FakeReadClient({"weeklyResults": results("p-qb1", "p-rb2")})
    confirmed, note = executor.confirm(payload)
    assert confirmed is False and "p-rb1" in note

    executor._read = FakeReadClient({"weeklyResults": {"weeklyResults": {}}})
    assert executor.confirm(payload)[0] is False
