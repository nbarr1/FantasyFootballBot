"""Recommendation records and the literal action payloads they carry.

A recommendation holds the *exact* request that would be sent to MFL, not a
prose summary of it. The user approves bytes, not a description, and the
approval token is bound to those bytes -- so what is shown and what is sent
cannot drift apart.
"""

from __future__ import annotations

import enum
import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from dataclasses import fields as dataclass_fields
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

from ..mfl.endpoints import Capability


class RecommendationKind(enum.StrEnum):
    LINEUP = "lineup"
    ADD_DROP = "add_drop"
    TRADE_PROPOSAL = "trade_proposal"
    TRADE_RESPONSE = "trade_response"


class RecommendationStatus(enum.StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    FAILED = "failed"
    EXPIRED = "expired"


class Confidence(enum.StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


def canonical_json(payload: dict[str, Any]) -> str:
    """Byte-stable serialisation. The approval token signs exactly this."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ActionPayload:
    """Base for the literal MFL request an approved recommendation would send."""

    capability: Capability

    #: Fields carried for display and rationale only -- they are shown to the
    #: user but never sent to MFL. Declared here so that the write client and
    #: the endpoint verifier agree on exactly which fields need a request
    #: parameter; when those two disagree, writes either block spuriously or
    #: send a field nobody vetted.
    DISPLAY_ONLY_FIELDS: ClassVar[frozenset[str]] = frozenset()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["capability"] = str(self.capability)
        return data

    @classmethod
    def transmitted_fields(cls) -> tuple[str, ...]:
        """Payload fields that must map to an MFL request parameter.

        Uses ``dataclasses.fields`` rather than ``__annotations__`` so that
        ClassVar declarations like ``DISPLAY_ONLY_FIELDS`` are not mistaken for
        payload data.
        """
        return tuple(
            f.name
            for f in dataclass_fields(cls)
            if f.name != "capability" and f.name not in cls.DISPLAY_ONLY_FIELDS
        )

    def request_params(self) -> dict[str, str]:
        """The query parameters for the MFL import call.

        Concrete parameter *names* are not known until
        ``bot verify-endpoints`` has read them from MFL's documentation, so
        subclasses build this from the verified endpoint's parameter list rather
        than hardcoding names here.
        """
        raise NotImplementedError

    def describe(self) -> str:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class LineupPayload(ActionPayload):
    league_id: str
    franchise_id: str
    week: int
    #: Player ids to start, in slot order.
    starter_ids: tuple[str, ...]
    #: Human-readable slot names parallel to ``starter_ids``, for display only.
    slot_names: tuple[str, ...] = ()

    DISPLAY_ONLY_FIELDS: ClassVar[frozenset[str]] = frozenset({"slot_names"})

    def describe(self) -> str:
        pairs = zip(self.slot_names or ("?",) * len(self.starter_ids), self.starter_ids)
        return (
            f"Set week {self.week} lineup for franchise {self.franchise_id}: "
            + ", ".join(f"{slot}={pid}" for slot, pid in pairs)
        )


@dataclass(frozen=True, slots=True)
class AddDropPayload(ActionPayload):
    league_id: str
    franchise_id: str
    add_player_id: str | None
    drop_player_id: str | None
    #: Blind-bid amount. None for FCFS and waiver-order leagues.
    bid_amount: float | None = None
    #: Recorded so the rationale can explain which workflow applies; MFL infers
    #: it from the league, so it is not transmitted.
    waiver_system: str = "unknown"

    DISPLAY_ONLY_FIELDS: ClassVar[frozenset[str]] = frozenset({"waiver_system"})

    def describe(self) -> str:
        bits = []
        if self.add_player_id:
            bits.append(f"ADD {self.add_player_id}")
        if self.drop_player_id:
            bits.append(f"DROP {self.drop_player_id}")
        if self.bid_amount is not None:
            bits.append(f"bid ${self.bid_amount:g}")
        return f"{' / '.join(bits)} (franchise {self.franchise_id})"


@dataclass(frozen=True, slots=True)
class TradeProposalPayload(ActionPayload):
    league_id: str
    franchise_id: str
    to_franchise_id: str
    #: Assets the user gives up and receives. Ids, exactly as MFL knows them.
    gives_player_ids: tuple[str, ...] = ()
    receives_player_ids: tuple[str, ...] = ()
    expires_days: int = 3
    message: str = ""

    def describe(self) -> str:
        return (
            f"Offer to franchise {self.to_franchise_id}: give "
            f"[{', '.join(self.gives_player_ids) or 'nothing'}] for "
            f"[{', '.join(self.receives_player_ids) or 'nothing'}]"
        )


@dataclass(frozen=True, slots=True)
class TradeResponsePayload(ActionPayload):
    league_id: str
    franchise_id: str
    offer_id: str
    accept: bool

    def describe(self) -> str:
        return f"{'ACCEPT' if self.accept else 'REJECT'} trade offer {self.offer_id}"


@dataclass(frozen=True, slots=True)
class Evidence:
    """The data behind a rationale, so a recommendation can be audited later."""

    projections: dict[str, float] = field(default_factory=dict)
    news_refs: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    notes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "projections": self.projections,
            "news_refs": list(self.news_refs),
            "sources": list(self.sources),
            "notes": self.notes,
        }


@dataclass(slots=True)
class Recommendation:
    kind: RecommendationKind
    payload: ActionPayload
    rationale: str
    evidence: Evidence
    confidence: Confidence
    caveats: tuple[str, ...] = ()
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime = field(
        default_factory=lambda: datetime.now(UTC) + timedelta(days=1)
    )
    status: RecommendationStatus = RecommendationStatus.PROPOSED

    @property
    def payload_dict(self) -> dict[str, Any]:
        return self.payload.to_dict()

    @property
    def payload_hash(self) -> str:
        return payload_hash(self.payload_dict)

    @property
    def is_expired(self) -> bool:
        return datetime.now(UTC) >= self.expires_at

    def render(self) -> str:
        """The full disclosure a user needs to decide: action, reasons, doubts."""
        lines = [
            f"[{self.id}] {self.kind.upper()}  ({self.confidence} confidence)",
            f"  Action : {self.payload.describe()}",
            f"  Expires: {self.expires_at:%Y-%m-%d %H:%M UTC}",
            "  Why    :",
        ]
        lines.extend(f"    {line}" for line in self.rationale.splitlines())
        if self.caveats:
            lines.append("  Caveats:")
            lines.extend(f"    - {c}" for c in self.caveats)
        lines.append("  Exact payload that would be sent to MFL:")
        for key, value in sorted(self.payload_dict.items()):
            lines.append(f"    {key} = {value!r}")
        return "\n".join(lines)
