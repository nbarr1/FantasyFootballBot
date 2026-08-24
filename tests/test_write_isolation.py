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
    from mflbot.approval.token import ApprovalToken
    from mflbot.config import LeagueRef
    from mflbot.mfl.auth import AuthState, Credentials
    from mflbot.recommend.models import LineupPayload
    from datetime import UTC, datetime, timedelta

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

    ``slot_names`` and ``waiver_system`` exist so the user can read what an
    action means. Sending them would put unvetted parameters on a real request.
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

    params = client._build_params(payload, registry.writes[Capability.SUBMIT_LINEUP])

    assert params == {
        "TYPE": "lineup",
        "L": "TEST0001",
        "W": "3",
        "FRANCHISE": "0001",
        "STARTERS": "p-qb1,p-rb1",
    }
    assert "QB,RB" not in params.values()
