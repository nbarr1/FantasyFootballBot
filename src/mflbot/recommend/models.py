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

    def wire_fields(self) -> dict[str, Any]:
        """Field name -> value, ready for the write client's ``field_map`` to
        turn into MFL request parameters.

        The default is the payload's own transmitted fields, verbatim. Override
        this only when a capability's *wire shape* depends on more than field
        names -- when the same dataclass serialises differently depending on
        which capability it carries (see :class:`AddDropPayload`), or a field's
        raw value needs composing into something MFL expects rather than a
        straight rename (a Unix-epoch integer from a ``datetime``, for
        instance, which the write client's generic value conversion already
        handles without an override here).

        May raise :class:`~mflbot.errors.PayloadIncomplete` if the payload does
        not carry enough information to build a valid request -- the write
        client calls this before consuming the approval token specifically so
        that failure here never spends a real approval.
        """
        return {name: getattr(self, name) for name in self.transmitted_fields()}

    def describe(self) -> str:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class LineupPayload(ActionPayload):
    league_id: str
    #: Which franchise this lineup is for. Not transmitted: MFL's docs describe
    #: FRANCHISE_ID as a commissioner-only override for acting on another
    #: owner's behalf, and this bot only ever acts as the authenticated owner,
    #: whose identity the session cookie already establishes.
    franchise_id: str
    week: int
    #: Player ids to start, in slot order.
    starter_ids: tuple[str, ...]
    #: Human-readable slot names parallel to ``starter_ids``, for display only.
    slot_names: tuple[str, ...] = ()

    DISPLAY_ONLY_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"slot_names", "franchise_id"}
    )

    def describe(self) -> str:
        # Every starter must appear, even if slot_names is short or absent.
        # Zipping the two directly would silently drop the trailing starters
        # from the description -- under-reporting what is about to be sent, on
        # the exact text the user reads before approving it.
        slots = self.slot_names + ("?",) * (
            len(self.starter_ids) - len(self.slot_names)
        )
        pairs = zip(slots, self.starter_ids, strict=True)
        return (
            f"Set week {self.week} lineup for franchise {self.franchise_id}: "
            + ", ".join(f"{slot}={pid}" for slot, pid in pairs)
        )


@dataclass(frozen=True, slots=True)
class AddDropPayload(ActionPayload):
    """One waiver/free-agent move.

    The dataclass is shared across three capabilities whose MFL wire shapes
    genuinely differ, not just in parameter names but in structure:

    * ``ADD_DROP_FCFS`` (``fcfsWaiver``) -- ADD and DROP are separate flat
      parameters.
    * ``WAIVER_CLAIM_ORDER`` (``waiverRequest``) and ``WAIVER_CLAIM_BBID``
      (``blindBidWaiverRequest``) -- add/drop(/bid) are packed into one
      underscore-joined ``PICKS`` string, MFL's own compound-claim format.

    :meth:`wire_fields` is the single place that difference is handled, so
    every other part of the bot -- the analyser, the approval display, the
    audit log -- deals in the same plain add/drop/bid fields regardless of
    which workflow applies.
    """

    league_id: str
    #: See LineupPayload.franchise_id -- identity comes from the session
    #: cookie; this bot never impersonates another franchise.
    franchise_id: str
    add_player_id: str | None
    drop_player_id: str | None
    #: Blind-bid amount. Required (and validated in wire_fields) for
    #: WAIVER_CLAIM_BBID; unused otherwise.
    bid_amount: float | None = None
    #: Waiver round. Required for WAIVER_CLAIM_ORDER (validated in
    #: wire_fields); optional for WAIVER_CLAIM_BBID, where MFL only asks for
    #: it in leagues using "conditional blind bidding" -- something this bot
    #: has no way to detect, so it is simply omitted from the request when
    #: unset here, which is the documented behaviour for the common case.
    round: int | None = None
    #: Recorded so the rationale can explain which workflow applies; MFL infers
    #: it from the league, so it is not transmitted.
    waiver_system: str = "unknown"

    DISPLAY_ONLY_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"waiver_system", "franchise_id"}
    )

    def describe(self) -> str:
        bits = []
        if self.add_player_id:
            bits.append(f"ADD {self.add_player_id}")
        if self.drop_player_id:
            bits.append(f"DROP {self.drop_player_id}")
        if self.bid_amount is not None:
            bits.append(f"bid ${self.bid_amount:g}")
        if self.round is not None:
            bits.append(f"round {self.round}")
        return f"{' / '.join(bits)} (franchise {self.franchise_id})"

    def wire_fields(self) -> dict[str, Any]:
        from ..errors import PayloadIncomplete
        from ..mfl.endpoints import Capability

        if self.capability is Capability.ADD_DROP_FCFS:
            out: dict[str, Any] = {"league_id": self.league_id}
            if self.add_player_id:
                out["add"] = self.add_player_id
            if self.drop_player_id:
                out["drop"] = self.drop_player_id
            return out

        if self.capability not in (
            Capability.WAIVER_CLAIM_ORDER, Capability.WAIVER_CLAIM_BBID
        ):
            raise AssertionError(f"AddDropPayload has no wire shape for {self.capability}")

        if not self.add_player_id:
            raise PayloadIncomplete("a waiver claim needs a player to add")
        # MFL's compound PICKS format: "<add>_<drop>", or with a bid amount
        # "<add>_<bid>_<drop>". "0000" is MFL's documented sentinel for "not
        # dropping anyone" inside this compound format -- unlike fcfsWaiver
        # above, DROP cannot simply be omitted here.
        drop = self.drop_player_id or "0000"
        if self.capability is Capability.WAIVER_CLAIM_BBID:
            if self.bid_amount is None:
                raise PayloadIncomplete(
                    "a blind-bid waiver claim needs a bid amount, and none is set "
                    "on this recommendation"
                )
            picks = f"{self.add_player_id}_{self.bid_amount:g}_{drop}"
        else:
            picks = f"{self.add_player_id}_{drop}"

        out = {"league_id": self.league_id, "picks": picks}
        if self.round is not None:
            out["round"] = self.round
        elif self.capability is Capability.WAIVER_CLAIM_ORDER:
            raise PayloadIncomplete(
                "this league uses waiver-order claims, which require a ROUND "
                "number, and none is set on this recommendation -- edit it "
                "(e.g. `bot edit <id> round=1`) with the round your league "
                "uses before approving"
            )
        return out


@dataclass(frozen=True, slots=True)
class TradeProposalPayload(ActionPayload):
    league_id: str
    franchise_id: str
    to_franchise_id: str
    #: Assets the user gives up and receives. Ids, exactly as MFL knows them.
    gives_player_ids: tuple[str, ...] = ()
    receives_player_ids: tuple[str, ...] = ()
    #: When the offer itself (not this approval) expires and can no longer be
    #: accepted. None means MFL applies its own default (one week from when
    #: offered) -- transmitted as a Unix-epoch integer when set, via the write
    #: client's generic datetime handling; not sent at all when None.
    expires_at: datetime | None = None
    message: str = ""

    DISPLAY_ONLY_FIELDS: ClassVar[frozenset[str]] = frozenset({"franchise_id"})

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
    #: MFL's own vocabulary, verbatim ('accept', 'reject', or 'revoke' -- the
    #: last only valid when the calling franchise originated the offer, which
    #: this bot's trade analyser never generates a recommendation for).
    response: str

    DISPLAY_ONLY_FIELDS: ClassVar[frozenset[str]] = frozenset({"franchise_id"})

    def describe(self) -> str:
        return f"{self.response.upper()} trade offer {self.offer_id}"


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
