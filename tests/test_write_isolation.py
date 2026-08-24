"""Writes must be unreachable from analysis code.

The design claim is that "analysis accidentally submits something to MFL" is not
a bug that can be written, because analysis code has no object with a write
method on it. These tests check that claim three ways:

1. **Statically** -- no module under ``mflbot/analysis`` or ``mflbot/ingest``
   imports the write client, transitively.
2. **Structurally** -- the read client exposes no write method, and the write
   client's only public entry point requires a token parameter.
3. **Behaviourally** -- a write attempted without a valid token does not reach
   the network, and neither does one whose endpoint is unverified.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from mflbot.errors import ApprovalError, EndpointNotVerifiedError
from mflbot.mfl.client import MFLReadClient
from mflbot.mfl.endpoints import Capability, EndpointRegistry, Provenance, WriteEndpoint
from mflbot.mfl.write_client import MFLWriteClient

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src" / "mflbot"
WRITE_MODULE = "mflbot.mfl.write_client"


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package_parts = path.relative_to(PACKAGE_ROOT).parts[:-1]
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # Resolve a relative import to its absolute module path.
                base = ["mflbot", *package_parts][: len(package_parts) + 1 - (node.level - 1)]
                module = ".".join([*base, node.module]) if node.module else ".".join(base)
            else:
                module = node.module or ""
            modules.add(module)
    return modules


def _reachable_from(start: Path, seen: set[str] | None = None) -> set[str]:
    """Transitively collect mflbot modules importable from ``start``."""
    seen = seen if seen is not None else set()
    for module in _imported_modules(start):
        if not module.startswith("mflbot") or module in seen:
            continue
        seen.add(module)
        candidate = PACKAGE_ROOT.parent / (module.replace(".", "/") + ".py")
        if candidate.exists():
            _reachable_from(candidate, seen)
    return seen


@pytest.mark.parametrize("package", ["analysis", "ingest"])
def test_analysis_and_ingest_cannot_reach_the_write_client(package: str) -> None:
    offenders = []
    for path in (PACKAGE_ROOT / package).rglob("*.py"):
        reachable = _reachable_from(path)
        if WRITE_MODULE in reachable:
            offenders.append(str(path.relative_to(PACKAGE_ROOT)))
    assert not offenders, (
        f"these {package} modules can reach the write client: {offenders}. "
        f"Analysis must not be able to submit to MFL."
    )


def test_read_client_has_no_write_methods() -> None:
    forbidden = {"submit", "post", "import_", "write", "propose", "set_lineup"}
    exposed = {name for name in dir(MFLReadClient) if not name.startswith("_")}
    assert not (exposed & forbidden), f"read client exposes writes: {exposed & forbidden}"


def test_the_only_public_write_entry_point_requires_a_token() -> None:
    public = {
        name
        for name, member in inspect.getmembers(MFLWriteClient, inspect.isfunction)
        if not name.startswith("_") and name not in {"close"}
    }
    assert public == {"submit"}, f"unexpected public write methods: {public}"
    parameters = inspect.signature(MFLWriteClient.submit).parameters
    assert "token" in parameters, "submit() must require an approval token"
    assert parameters["token"].default is inspect.Parameter.empty, (
        "the approval token must not have a default -- it cannot be optional"
    )


def test_unverified_endpoint_blocks_the_write_before_anything_is_sent(
    db, tokens, synthetic_settings
) -> None:
    from mflbot.config import LeagueRef
    from mflbot.mfl.auth import AuthState, Credentials
    from mflbot.recommend.models import LineupPayload

    class ExplodingTransport:
        def post(self, *args, **kwargs):  # pragma: no cover - must never run
            raise AssertionError("a request was sent for an unverified endpoint")

    client = MFLWriteClient(
        LeagueRef("TEST0001", 2026, "example.invalid"),
        AuthState(Credentials(username="u", password="p"), session_cookie="cookie"),
        tokens,
        registry=EndpointRegistry(),  # nothing verified
        transport=ExplodingTransport(),
    )
    payload = LineupPayload(
        capability=Capability.SUBMIT_LINEUP,
        league_id="TEST0001",
        franchise_id="0001",
        week=1,
        starter_ids=("p-qb1",),
    )

    class FakeToken:
        token_id = "irrelevant"

    with pytest.raises(EndpointNotVerifiedError):
        client.submit(payload, FakeToken())


def test_forged_token_blocks_the_write_before_anything_is_sent(db, tokens) -> None:
    """Even with a fully verified endpoint, a bad token stops the request."""
    from datetime import UTC, datetime, timedelta

    from mflbot.approval.token import ApprovalToken
    from mflbot.config import LeagueRef
    from mflbot.mfl.auth import AuthState, Credentials
    from mflbot.recommend.models import LineupPayload

    class ExplodingTransport:
        def post(self, *args, **kwargs):  # pragma: no cover - must never run
            raise AssertionError("a request was sent with an unverified token")

    registry = EndpointRegistry()
    registry.writes[Capability.SUBMIT_LINEUP] = WriteEndpoint(
        capability=Capability.SUBMIT_LINEUP,
        candidates=("lineup",),
        description="synthetic verified endpoint",
        type_name="lineup",
        params=("L", "W", "FRANCHISE", "STARTERS"),
        provenance=Provenance.DOC_VERIFIED,
        field_map={
            "league_id": "L",
            "week": "W",
            "franchise_id": "FRANCHISE",
            "starter_ids": "STARTERS",
        },
    )

    client = MFLWriteClient(
        LeagueRef("TEST0001", 2026, "example.invalid"),
        AuthState(Credentials(username="u", password="p"), session_cookie="cookie"),
        tokens,
        registry=registry,
        transport=ExplodingTransport(),
    )
    payload = LineupPayload(
        capability=Capability.SUBMIT_LINEUP,
        league_id="TEST0001",
        franchise_id="0001",
        week=1,
        starter_ids=("p-qb1",),
    )
    never_issued = ApprovalToken(
        token_id="forged",
        recommendation_id="nope",
        payload_hash="deadbeef",
        signature="0" * 64,
        issued_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        approved_by="attacker",
    )

    with pytest.raises(ApprovalError):
        client.submit(payload, never_issued)


def test_api_key_alone_cannot_write() -> None:
    """Writes require a real user session, not just a private-league read key."""
    from mflbot.errors import AuthError
    from mflbot.mfl.auth import AuthState, Credentials

    auth = AuthState(Credentials(api_key="a-key"))
    assert auth.is_authenticated is True
    assert auth.can_write is False
    with pytest.raises(AuthError, match="logged-in MFL user session"):
        auth.require_writable("submitting a lineup")


def test_display_only_fields_are_never_transmitted(db, tokens) -> None:
    """Fields carried for the user's benefit must not reach MFL.

    ``slot_names`` exists so the user can read what an action means, and
    ``franchise_id`` is display-only across every write payload -- MFL's docs
    describe FRANCHISE_ID as a commissioner-only impersonation override, and
    this bot's identity always comes from the session cookie. Even with a
    field_map entry present for franchise_id (proving the omission isn't just
    "nobody wrote the mapping yet"), it must never appear in what gets sent.
    """
    from mflbot.config import LeagueRef
    from mflbot.mfl.auth import AuthState, Credentials
    from mflbot.recommend.models import LineupPayload

    registry = EndpointRegistry()
    registry.writes[Capability.SUBMIT_LINEUP] = WriteEndpoint(
        capability=Capability.SUBMIT_LINEUP,
        candidates=("lineup",),
        description="synthetic verified endpoint",
        type_name="lineup",
        params=("L", "W", "FRANCHISE", "STARTERS"),
        provenance=Provenance.DOC_VERIFIED,
        field_map={
            "league_id": "L",
            "week": "W",
            "franchise_id": "FRANCHISE",
            "starter_ids": "STARTERS",
        },
    )
    client = MFLWriteClient(
        LeagueRef("TEST0001", 2026, "example.invalid"),
        AuthState(Credentials(username="u", password="p"), session_cookie="cookie"),
        tokens,
        registry=registry,
        transport=object(),
    )
    payload = LineupPayload(
        capability=Capability.SUBMIT_LINEUP,
        league_id="TEST0001",
        franchise_id="0001",
        week=3,
        starter_ids=("p-qb1", "p-rb1"),
        slot_names=("QB", "RB"),
    )

    wire = payload.wire_fields()
    assert "franchise_id" not in wire and "slot_names" not in wire

    params = client._params_from_wire(wire, registry.writes[Capability.SUBMIT_LINEUP])

    assert params == {
        "TYPE": "lineup",
        "L": "TEST0001",
        "W": "3",
        "STARTERS": "p-qb1,p-rb1",
    }
    assert "0001" not in params.values(), "franchise_id leaked despite being display-only"
    assert "QB,RB" not in params.values()


# -- real, hand-verified wire shapes (from MFL's Request Reference Page) ---

from mflbot.config import LeagueRef  # noqa: E402
from mflbot.mfl.auth import AuthState, Credentials  # noqa: E402


def _issue_token(store, tokens, payload, actor="test"):
    from mflbot.recommend.models import Confidence, Evidence, Recommendation, RecommendationKind

    kind_by_capability = {
        Capability.SUBMIT_LINEUP: "lineup",
        Capability.ADD_DROP_FCFS: "add_drop",
        Capability.WAIVER_CLAIM_ORDER: "add_drop",
        Capability.WAIVER_CLAIM_BBID: "add_drop",
        Capability.PROPOSE_TRADE: "trade_proposal",
        Capability.RESPOND_TO_TRADE: "trade_response",
    }
    recommendation = Recommendation(
        kind=RecommendationKind(kind_by_capability[payload.capability]),
        payload=payload, rationale="synthetic", evidence=Evidence(),
        confidence=Confidence.HIGH,
    )
    store.save(recommendation)
    return recommendation, tokens.issue(recommendation, actor)


def _real_registry():
    return EndpointRegistry.load("endpoints.lock.json")


def test_lock_file_unlocks_all_six_capabilities() -> None:
    registry = _real_registry()
    assert registry.unverified_writes() == ()
    assert registry.write(Capability.ADD_DROP_FCFS).type_name == "fcfsWaiver"
    assert registry.write(Capability.WAIVER_CLAIM_BBID).type_name == "blindBidWaiverRequest"
    assert registry.write(Capability.WAIVER_CLAIM_ORDER).type_name == "waiverRequest"
    assert registry.write(Capability.PROPOSE_TRADE).type_name == "tradeProposal"
    assert registry.write(Capability.RESPOND_TO_TRADE).type_name == "tradeResponse"
    assert registry.write(Capability.SUBMIT_LINEUP).type_name == "lineup"


def test_fcfs_wire_shape_matches_mfls_flat_add_drop_params(db, store, tokens) -> None:
    from mflbot.recommend.models import AddDropPayload

    payload = AddDropPayload(
        Capability.ADD_DROP_FCFS, "TEST0001", "0001", "p-fa1", "p-rb2"
    )
    recommendation, token = _issue_token(store, tokens, payload)
    posted = {}

    class Recorder:
        def post(self, url, data=None, headers=None):
            posted["data"] = data
            return type("R", (), {"status_code": 200, "text": "OK"})()
        def close(self): return None

    client = MFLWriteClient(
        LeagueRef("TEST0001", 2026, "example.invalid"),
        AuthState(Credentials(username="u", password="p"), session_cookie="c"),
        tokens, registry=_real_registry(), transport=Recorder(),
    )
    client.submit(payload, token)
    assert posted["data"] == {
        "TYPE": "fcfsWaiver", "L": "TEST0001", "ADD": "p-fa1", "DROP": "p-rb2",
    }


def test_bbid_wire_shape_matches_mfls_compound_picks_format(db, store, tokens) -> None:
    """"<add>_<bid>_<drop>", with MFL's documented "0000" sentinel when not
    dropping anyone -- the exact shape from the Request Reference Page."""
    from mflbot.recommend.models import AddDropPayload

    payload = AddDropPayload(
        Capability.WAIVER_CLAIM_BBID, "TEST0001", "0001", "p-fa1", None,
        bid_amount=12.5,
    )
    recommendation, token = _issue_token(store, tokens, payload)
    posted = {}

    class Recorder:
        def post(self, url, data=None, headers=None):
            posted["data"] = data
            return type("R", (), {"status_code": 200, "text": "OK"})()
        def close(self): return None

    client = MFLWriteClient(
        LeagueRef("TEST0001", 2026, "example.invalid"),
        AuthState(Credentials(username="u", password="p"), session_cookie="c"),
        tokens, registry=_real_registry(), transport=Recorder(),
    )
    client.submit(payload, token)
    assert posted["data"]["PICKS"] == "p-fa1_12.5_0000"


def test_an_incomplete_bbid_claim_never_consumes_the_token(db, store, tokens) -> None:
    """A blind-bid claim with no bid amount cannot become a valid request.
    The token must survive untouched, so a corrected payload can still use it
    -- no, it must be re-approved, but critically it must not be silently
    burned on a submission that never actually reached MFL."""
    from mflbot.errors import PayloadIncomplete
    from mflbot.recommend.models import AddDropPayload

    payload = AddDropPayload(
        Capability.WAIVER_CLAIM_BBID, "TEST0001", "0001", "p-fa1", None,
        bid_amount=None,
    )
    recommendation, token = _issue_token(store, tokens, payload)

    class ExplodingTransport:
        def post(self, *a, **kw):
            raise AssertionError("a request was sent for an incomplete payload")
        def close(self): return None

    client = MFLWriteClient(
        LeagueRef("TEST0001", 2026, "example.invalid"),
        AuthState(Credentials(username="u", password="p"), session_cookie="c"),
        tokens, registry=_real_registry(), transport=ExplodingTransport(),
    )
    with pytest.raises(PayloadIncomplete):
        client.submit(payload, token)

    # The token must still be usable: nothing was actually sent.
    row = db.query_one(
        "SELECT consumed_at FROM approval_tokens WHERE token_id=?", (token.token_id,)
    )
    assert row["consumed_at"] is None, "an incomplete payload must not burn the token"


def test_waiver_order_claim_without_a_round_never_consumes_the_token(db, store, tokens) -> None:
    from mflbot.errors import PayloadIncomplete
    from mflbot.recommend.models import AddDropPayload

    payload = AddDropPayload(
        Capability.WAIVER_CLAIM_ORDER, "TEST0001", "0001", "p-fa1", "p-rb2",
    )
    recommendation, token = _issue_token(store, tokens, payload)

    class ExplodingTransport:
        def post(self, *a, **kw):
            raise AssertionError("a request was sent with no ROUND set")
        def close(self): return None

    client = MFLWriteClient(
        LeagueRef("TEST0001", 2026, "example.invalid"),
        AuthState(Credentials(username="u", password="p"), session_cookie="c"),
        tokens, registry=_real_registry(), transport=ExplodingTransport(),
    )
    with pytest.raises(PayloadIncomplete, match="ROUND"):
        client.submit(payload, token)
    row = db.query_one(
        "SELECT consumed_at FROM approval_tokens WHERE token_id=?", (token.token_id,)
    )
    assert row["consumed_at"] is None


def test_waiver_order_claim_with_a_round_builds_correctly(db, store, tokens) -> None:
    from mflbot.recommend.models import AddDropPayload

    payload = AddDropPayload(
        Capability.WAIVER_CLAIM_ORDER, "TEST0001", "0001", "p-fa1", "p-rb2",
        round=3,
    )
    recommendation, token = _issue_token(store, tokens, payload)
    posted = {}

    class Recorder:
        def post(self, url, data=None, headers=None):
            posted["data"] = data
            return type("R", (), {"status_code": 200, "text": "OK"})()
        def close(self): return None

    client = MFLWriteClient(
        LeagueRef("TEST0001", 2026, "example.invalid"),
        AuthState(Credentials(username="u", password="p"), session_cookie="c"),
        tokens, registry=_real_registry(), transport=Recorder(),
    )
    client.submit(payload, token)
    assert posted["data"] == {
        "TYPE": "waiverRequest", "L": "TEST0001", "ROUND": "3", "PICKS": "p-fa1_p-rb2",
    }


def test_trade_response_sends_mfls_own_literal_vocabulary(db, store, tokens) -> None:
    """RESPONSE must be the literal string 'accept'/'reject', not "1"/"0" --
    the generic bool-to-"1"/"0" serialisation would be wrong here, which is
    exactly why TradeResponsePayload carries MFL's own vocabulary directly
    rather than a bool the write client would have to reinterpret."""
    from mflbot.recommend.models import TradeResponsePayload

    payload = TradeResponsePayload(
        Capability.RESPOND_TO_TRADE, "TEST0001", "0001", "offer-9", "accept"
    )
    recommendation, token = _issue_token(store, tokens, payload)
    posted = {}

    class Recorder:
        def post(self, url, data=None, headers=None):
            posted["data"] = data
            return type("R", (), {"status_code": 200, "text": "OK"})()
        def close(self): return None

    client = MFLWriteClient(
        LeagueRef("TEST0001", 2026, "example.invalid"),
        AuthState(Credentials(username="u", password="p"), session_cookie="c"),
        tokens, registry=_real_registry(), transport=Recorder(),
    )
    client.submit(payload, token)
    assert posted["data"]["RESPONSE"] == "accept"
    assert posted["data"] == {
        "TYPE": "tradeResponse", "L": "TEST0001", "TRADE_ID": "offer-9",
        "RESPONSE": "accept",
    }


def test_trade_proposal_expiry_becomes_a_unix_epoch_not_an_iso_string(db, store, tokens) -> None:
    from datetime import UTC, datetime

    from mflbot.recommend.models import TradeProposalPayload

    payload = TradeProposalPayload(
        Capability.PROPOSE_TRADE, "TEST0001", "0001", "0002",
        gives_player_ids=("p1",), receives_player_ids=("p2",),
        expires_at=datetime(2026, 9, 1, tzinfo=UTC),
    )
    recommendation, token = _issue_token(store, tokens, payload)
    posted = {}

    class Recorder:
        def post(self, url, data=None, headers=None):
            posted["data"] = data
            return type("R", (), {"status_code": 200, "text": "OK"})()
        def close(self): return None

    client = MFLWriteClient(
        LeagueRef("TEST0001", 2026, "example.invalid"),
        AuthState(Credentials(username="u", password="p"), session_cookie="c"),
        tokens, registry=_real_registry(), transport=Recorder(),
    )
    client.submit(payload, token)
    assert posted["data"]["EXPIRES"] == str(int(datetime(2026, 9, 1, tzinfo=UTC).timestamp()))


def test_trade_proposal_with_no_expiry_omits_the_parameter(db, store, tokens) -> None:
    """None means let MFL apply its own default (one week); it must not be
    sent as the literal string 'None'."""
    from mflbot.recommend.models import TradeProposalPayload

    payload = TradeProposalPayload(
        Capability.PROPOSE_TRADE, "TEST0001", "0001", "0002",
        gives_player_ids=("p1",), receives_player_ids=("p2",),
    )
    recommendation, token = _issue_token(store, tokens, payload)
    posted = {}

    class Recorder:
        def post(self, url, data=None, headers=None):
            posted["data"] = data
            return type("R", (), {"status_code": 200, "text": "OK"})()
        def close(self): return None

    client = MFLWriteClient(
        LeagueRef("TEST0001", 2026, "example.invalid"),
        AuthState(Credentials(username="u", password="p"), session_cookie="c"),
        tokens, registry=_real_registry(), transport=Recorder(),
    )
    client.submit(payload, token)
    assert "EXPIRES" not in posted["data"]


class _RecordingTransport:
    """Stores the last POST body on the instance, not a closed-over loop var."""

    def __init__(self) -> None:
        self.posted: dict | None = None

    def post(self, url, data=None, headers=None):
        self.posted = data
        return type("R", (), {"status_code": 200, "text": "OK"})()

    def close(self) -> None:
        return None


def test_franchise_id_is_never_sent_on_any_real_write(db, store, tokens) -> None:
    """MFL describes FRANCHISE_ID as a commissioner impersonation override.
    This bot always acts as the authenticated owner; it must never send it."""
    from mflbot.recommend.models import AddDropPayload, LineupPayload

    cases = [
        LineupPayload(Capability.SUBMIT_LINEUP, "TEST0001", "0007", 1, ("p1",)),
        AddDropPayload(Capability.ADD_DROP_FCFS, "TEST0001", "0007", "p1", None),
    ]
    registry = _real_registry()
    for payload in cases:
        recommendation, token = _issue_token(store, tokens, payload)
        transport = _RecordingTransport()
        client = MFLWriteClient(
            LeagueRef("TEST0001", 2026, "example.invalid"),
            AuthState(Credentials(username="u", password="p"), session_cookie="c"),
            tokens, registry=registry, transport=transport,
        )
        client.submit(payload, token)
        assert "0007" not in transport.posted.values(), transport.posted
