"""Execution: preconditions, audit trail, and confirmation by re-reading.

Everything here uses fakes for MFL. The point is the executor's own behaviour
around the write, which is where the safety properties live.
"""

from __future__ import annotations

from types import SimpleNamespace

from mflbot.execute.executor import Executor
from mflbot.mfl.endpoints import Capability
from mflbot.mfl.write_client import WriteResult
from mflbot.recommend.models import (
    AddDropPayload,
    Confidence,
    Evidence,
    LineupPayload,
    Recommendation,
    RecommendationKind,
    RecommendationStatus,
)


class FakeLeague:
    id = "TEST0001"
    season = 2026


def free_agents_payload(*player_ids):
    """Body of a freeAgents export, in the shape MFL actually returns."""
    return {"freeAgents": {"leagueUnit": {"player": [{"id": p} for p in player_ids]}}}


def rosters_payload(franchise_id, *player_ids):
    """Body of a rosters export, in the shape MFL actually returns."""
    return {
        "rosters": {
            "franchise": {
                "id": franchise_id,
                "player": [{"id": p, "status": "ROSTER"} for p in player_ids],
            }
        }
    }


class FakeReadClient:
    """Serves canned export payloads and records what was re-read."""

    def __init__(self, payloads=None, pending_trades=None) -> None:
        self.league = FakeLeague()
        self._payloads = payloads or {}
        self._pending_trades = pending_trades or {}
        self.reads: list[str] = []

    def export(self, type_name, **kwargs):
        """Return the full response envelope for ``type_name``.

        Payloads are registered as complete envelopes (``{"rosters": {...}}``),
        the same shape the real client hands back, so the production parsers are
        exercised rather than bypassed.
        """
        self.reads.append(type_name)
        return SimpleNamespace(payload=self._payloads.get(type_name, {}))

    def pending_trades(self, franchise=None, **kwargs):
        self.reads.append("pendingTrades")
        return self._pending_trades


class FakeWriteClient:
    def __init__(self, result=None, raises=None) -> None:
        self.result = result
        self.raises = raises
        self.calls: list[tuple] = []

    def submit(self, payload, token):
        self.calls.append((payload, token))
        if self.raises:
            raise self.raises
        return self.result or WriteResult(
            capability=payload.capability,
            endpoint_type="fake",
            request_summary="import?TYPE=fake",
            http_status=200,
            body="OK",
            succeeded=True,
        )


class FakeToken:
    token_id = "tok-1"


def add_drop_recommendation(store, **kw):
    payload = AddDropPayload(
        capability=Capability.ADD_DROP_FCFS,
        league_id="TEST0001",
        franchise_id="0001",
        add_player_id="p-fa1",
        drop_player_id="p-rb2",
        **kw,
    )
    recommendation = Recommendation(
        kind=RecommendationKind.ADD_DROP,
        payload=payload,
        rationale="synthetic",
        evidence=Evidence(),
        confidence=Confidence.MEDIUM,
    )
    store.save(recommendation)
    return recommendation


def test_claimed_player_aborts_the_write_and_is_audited(repos, store) -> None:
    """The world moved after approval: abandon, do not adapt."""
    recommendation = add_drop_recommendation(store)
    # The free-agent re-read no longer contains the target player.
    read = FakeReadClient({"freeAgents": free_agents_payload("p-other")})
    write = FakeWriteClient()
    executor = Executor(write, read, repos, store)

    outcome = executor.execute(recommendation, FakeToken())

    assert write.calls == [], "nothing may be submitted once a precondition fails"
    assert not outcome.submitted
    assert "no longer a free agent" in outcome.message
    assert store.get(recommendation.id).status == RecommendationStatus.FAILED

    entries = repos.audit_entries()
    assert any(e["outcome"] == "refused" for e in entries)


def test_expired_recommendation_is_never_submitted(repos, store) -> None:
    from datetime import UTC, datetime, timedelta

    recommendation = add_drop_recommendation(store)
    recommendation.expires_at = datetime.now(UTC) - timedelta(hours=1)
    store.save(recommendation)

    write = FakeWriteClient()
    executor = Executor(write, FakeReadClient(), repos, store)
    outcome = executor.execute(recommendation, FakeToken())

    assert write.calls == []
    assert not outcome.submitted


def test_lineup_past_lock_is_refused(repos, store, synthetic_settings) -> None:
    import dataclasses
    from datetime import UTC, datetime, timedelta

    locked = dataclasses.replace(
        synthetic_settings,
        league_id="TEST0001",
        lineup_deadline=datetime.now(UTC) - timedelta(minutes=5),
    )
    repos.save_league_settings(locked, {"league": {}})

    payload = LineupPayload(
        capability=Capability.SUBMIT_LINEUP,
        league_id="TEST0001",
        franchise_id="0001",
        week=5,
        starter_ids=("p-qb1",),
    )
    recommendation = Recommendation(
        kind=RecommendationKind.LINEUP, payload=payload, rationale="synthetic",
        evidence=Evidence(), confidence=Confidence.HIGH,
    )
    store.save(recommendation)

    write = FakeWriteClient()
    executor = Executor(write, FakeReadClient(), repos, store)
    outcome = executor.execute(recommendation, FakeToken())

    assert write.calls == []
    assert "locked" in outcome.message


def test_successful_write_is_confirmed_by_re_reading_league_state(repos, store) -> None:
    recommendation = add_drop_recommendation(store)
    read = FakeReadClient(
        # After the write, the roster shows the add and not the drop.
        {
            "freeAgents": free_agents_payload("p-fa1"),
            "rosters": rosters_payload("0001", "p-fa1"),
        }
    )
    executor = Executor(FakeWriteClient(), read, repos, store)

    outcome = executor.execute(recommendation, FakeToken())

    assert outcome.submitted and outcome.confirmed
    assert "rosters" in read.reads, "confirmation must re-read, not trust the response"
    assert store.get(recommendation.id).status == RecommendationStatus.EXECUTED


def test_accepted_but_unconfirmed_write_is_reported_honestly(repos, store) -> None:
    """MFL saying OK is not proof. Say so rather than claiming success."""
    recommendation = add_drop_recommendation(store)
    read = FakeReadClient(
        # The roster does NOT reflect the add (e.g. a queued waiver claim).
        {
            "freeAgents": free_agents_payload("p-fa1"),
            "rosters": rosters_payload("0001", "p-rb2"),
        }
    )
    executor = Executor(FakeWriteClient(), read, repos, store)

    outcome = executor.execute(recommendation, FakeToken())

    assert outcome.submitted and not outcome.confirmed
    assert "did not confirm" in outcome.message


def test_rejected_write_is_not_retried(repos, store) -> None:
    recommendation = add_drop_recommendation(store)
    read = FakeReadClient({"freeAgents": free_agents_payload("p-fa1")})
    write = FakeWriteClient(
        result=WriteResult(
            capability=Capability.ADD_DROP_FCFS,
            endpoint_type="fake",
            request_summary="import?TYPE=fake",
            http_status=200,
            body="error: roster is full",
            succeeded=False,
        )
    )
    executor = Executor(write, read, repos, store)

    outcome = executor.execute(recommendation, FakeToken())

    assert len(write.calls) == 1, "a rejected write must not be retried automatically"
    assert not outcome.ok
    assert "No retry was attempted" in outcome.message
    assert store.get(recommendation.id).status == RecommendationStatus.FAILED


def test_every_attempt_leaves_an_audit_trail(repos, store) -> None:
    recommendation = add_drop_recommendation(store)
    read = FakeReadClient(
        {
            "freeAgents": free_agents_payload("p-fa1"),
            "rosters": rosters_payload("0001", "p-fa1"),
        }
    )
    executor = Executor(FakeWriteClient(), read, repos, store)
    executor.execute(recommendation, FakeToken())

    entries = repos.audit_entries()
    outcomes = [e["outcome"] for e in entries]
    assert "submitting" in outcomes
    assert "confirmed" in outcomes
    assert all(e["recommendation_id"] == recommendation.id for e in entries)


def test_audit_entries_never_contain_credentials(repos, store) -> None:
    recommendation = add_drop_recommendation(store)
    read = FakeReadClient({"freeAgents": free_agents_payload("p-fa1")})
    write = FakeWriteClient(
        result=WriteResult(
            capability=Capability.ADD_DROP_FCFS,
            endpoint_type="fake",
            request_summary="import?TYPE=fake&APIKEY=sekret&PASSWORD=hunter2",
            http_status=200,
            body="OK",
            succeeded=True,
        )
    )
    executor = Executor(write, read, repos, store)
    executor.execute(recommendation, FakeToken())

    blob = str(repos.audit_entries())
    assert "sekret" not in blob and "hunter2" not in blob
