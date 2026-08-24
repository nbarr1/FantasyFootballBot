"""The approval invariant.

These tests encode the bot's central promise: nothing reaches MFL without an
explicit, per-action approval, and an approval authorises exactly one
submission of exactly one payload.
"""

from __future__ import annotations

import dataclasses

import pytest

from mflbot.approval.cli_channel import CLIApprovalChannel
from mflbot.errors import ApprovalError
from mflbot.mfl.endpoints import Capability
from mflbot.recommend.models import (
    AddDropPayload,
    Confidence,
    Evidence,
    LineupPayload,
    Recommendation,
    RecommendationKind,
    RecommendationStatus,
    payload_hash,
)


def make_recommendation(store, **overrides):
    payload = LineupPayload(
        capability=Capability.SUBMIT_LINEUP,
        league_id="TEST0001",
        franchise_id="0001",
        week=5,
        starter_ids=("p-qb1", "p-rb1"),
        slot_names=("QB", "RB"),
    )
    recommendation = Recommendation(
        kind=RecommendationKind.LINEUP,
        payload=payload,
        rationale="synthetic rationale",
        evidence=Evidence(),
        confidence=Confidence.MEDIUM,
        **overrides,
    )
    store.save(recommendation)
    return recommendation


def test_valid_token_verifies(store, tokens) -> None:
    recommendation = make_recommendation(store)
    token = tokens.issue(recommendation, "test")
    tokens.verify(token, recommendation.payload_hash)  # must not raise


def test_token_is_single_use(store, tokens) -> None:
    recommendation = make_recommendation(store)
    token = tokens.issue(recommendation, "test")
    tokens.consume(token, recommendation.payload_hash)
    with pytest.raises(ApprovalError, match="already used"):
        tokens.consume(token, recommendation.payload_hash)


def test_token_does_not_authorise_a_different_payload(store, tokens) -> None:
    recommendation = make_recommendation(store)
    token = tokens.issue(recommendation, "test")
    tampered = dataclasses.replace(
        recommendation.payload, starter_ids=("p-qb2", "p-rb2")
    )
    with pytest.raises(ApprovalError, match="different action"):
        tokens.verify(token, payload_hash(tampered.to_dict()))


def test_forged_signature_is_rejected(store, tokens) -> None:
    recommendation = make_recommendation(store)
    token = tokens.issue(recommendation, "test")
    forged = dataclasses.replace(token, signature="0" * 64)
    with pytest.raises(ApprovalError, match="invalid signature"):
        tokens.verify(forged, recommendation.payload_hash)


def test_token_from_a_different_signing_key_is_rejected(db, store) -> None:
    from mflbot.approval.token import TokenService

    issuer = TokenService(db=db, secret=b"attacker-key")
    verifier = TokenService(db=db, secret=b"real-key")
    recommendation = make_recommendation(store)
    token = issuer.issue(recommendation, "attacker")
    with pytest.raises(ApprovalError, match="invalid signature"):
        verifier.verify(token, recommendation.payload_hash)


def test_expired_recommendation_cannot_be_approved(store, tokens) -> None:
    from datetime import UTC, datetime, timedelta

    recommendation = make_recommendation(
        store, expires_at=datetime.now(UTC) - timedelta(hours=1)
    )
    with pytest.raises(ApprovalError, match="expired"):
        tokens.issue(recommendation, "test")


def test_token_never_outlives_the_recommendation(store, tokens) -> None:
    """A lineup token must die at lock, not at the token TTL."""
    from datetime import UTC, datetime, timedelta

    lock = datetime.now(UTC) + timedelta(minutes=30)
    recommendation = make_recommendation(store, expires_at=lock)
    token = tokens.issue(recommendation, "test")
    assert token.expires_at == lock


def test_silence_expires_and_never_executes(store) -> None:
    from datetime import UTC, datetime, timedelta

    make_recommendation(store, expires_at=datetime.now(UTC) - timedelta(seconds=1))
    assert store.expire_stale() == 1
    assert store.pending() == []
    statuses = {r.status for r in store.all()}
    assert statuses == {RecommendationStatus.EXPIRED}
    assert RecommendationStatus.APPROVED not in statuses


def test_approving_one_item_leaves_others_untouched(store, tokens) -> None:
    first = make_recommendation(store)
    second = make_recommendation(store)
    channel = CLIApprovalChannel(store, tokens, writer=lambda _: None)

    channel.approve(first.id)

    assert store.get(first.id).status == RecommendationStatus.APPROVED
    assert store.get(second.id).status == RecommendationStatus.PROPOSED


def test_editing_invalidates_an_earlier_approval(store, tokens) -> None:
    recommendation = make_recommendation(store)
    channel = CLIApprovalChannel(store, tokens, writer=lambda _: None)

    decision = channel.approve(recommendation.id)
    old_token = decision.token

    channel.edit(recommendation.id, {"starter_ids": "p-qb2,p-rb2"})
    edited = store.get(recommendation.id)

    with pytest.raises(ApprovalError, match="different action"):
        tokens.verify(old_token, edited.payload_hash)


def test_identity_fields_cannot_be_edited(store, tokens) -> None:
    """Retargeting an approved action at another team is not an 'edit'."""
    payload = AddDropPayload(
        capability=Capability.ADD_DROP_FCFS,
        league_id="TEST0001",
        franchise_id="0001",
        add_player_id="p-rb3",
        drop_player_id="p-te2",
    )
    recommendation = Recommendation(
        kind=RecommendationKind.ADD_DROP,
        payload=payload,
        rationale="synthetic",
        evidence=Evidence(),
        confidence=Confidence.LOW,
    )
    store.save(recommendation)
    channel = CLIApprovalChannel(store, tokens, writer=lambda _: None)

    for field in ("league_id", "franchise_id"):
        with pytest.raises(ApprovalError, match="cannot be edited"):
            channel.edit(recommendation.id, {field: "9999"})


def test_rejection_produces_no_token(store, tokens) -> None:
    recommendation = make_recommendation(store)
    channel = CLIApprovalChannel(store, tokens, writer=lambda _: None)
    decision = channel.reject(recommendation.id, note="no thanks")
    assert decision.token is None
    assert not decision.authorises_execution
    assert store.get(recommendation.id).status == RecommendationStatus.REJECTED


def test_a_recommendation_cannot_be_approved_twice(store, tokens) -> None:
    recommendation = make_recommendation(store)
    channel = CLIApprovalChannel(store, tokens, writer=lambda _: None)
    channel.approve(recommendation.id)
    with pytest.raises(ApprovalError, match="already"):
        channel.approve(recommendation.id)


def test_lineup_description_lists_every_starter_being_submitted() -> None:
    """The description must never under-report the action.

    It is the text the user reads before approving a real submission, so a
    starter missing from it means approving something other than what was
    shown. Slot names are cosmetic and may be short or absent; the starter list
    is not.
    """
    for slot_names in ((), ("QB",), ("QB", "RB"), ("QB", "RB", "WR")):
        payload = LineupPayload(
            capability=Capability.SUBMIT_LINEUP,
            league_id="TEST0001",
            franchise_id="0001",
            week=5,
            starter_ids=("p-qb1", "p-rb1", "p-wr1"),
            slot_names=slot_names,
        )
        described = payload.describe()
        missing = [p for p in payload.starter_ids if p not in described]
        assert not missing, (
            f"slot_names={slot_names!r} dropped {missing} from the description"
        )
