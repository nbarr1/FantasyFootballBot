"""Registry of MFL API endpoints, with explicit provenance for each entry.

Why this module exists
----------------------
MFL's own developer documentation (``/{season}/api_info``) is the only
authoritative source for endpoint names, parameters and response shapes. It was
**not reachable from the machine that generated this code**, so nothing here may
be treated as confirmed simply because it is written down.

Each endpoint therefore carries a :class:`Provenance`:

``DOC_VERIFIED``
    Reconciled against the live ``api_info`` page by ``bot verify-endpoints``
    and pinned in ``endpoints.lock.json``.
``THIRD_PARTY_CLIENT``
    Taken from an independently written, working open-source MFL client and
    corroborated by a second source. Good enough for *reads*: a wrong name
    yields an obvious, loud failure and no side effects.
``UNVERIFIED``
    Not confirmed anywhere. **Never usable for writes.**

Read endpoints may be called at ``THIRD_PARTY_CLIENT`` provenance. Write
endpoints require ``DOC_VERIFIED``; calling one before verification raises
:class:`~mflbot.errors.EndpointNotVerifiedError`. This is the fail-closed rule
applied to the API surface itself: the bot will not POST a guess at a league.
"""

from __future__ import annotations

import enum
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import EndpointNotVerifiedError

LOCK_FILENAME = "endpoints.lock.json"


class Provenance(enum.StrEnum):
    DOC_VERIFIED = "doc_verified"
    THIRD_PARTY_CLIENT = "third_party_client"
    UNVERIFIED = "unverified"


class Capability(enum.StrEnum):
    """What a write endpoint *does*, independent of what MFL calls it.

    Analysis and approval code refers to capabilities. The mapping from
    capability to a concrete ``TYPE`` value is resolved only by verification
    against the live documentation.
    """

    SUBMIT_LINEUP = "submit_lineup"
    ADD_DROP_FCFS = "add_drop_fcfs"
    WAIVER_CLAIM_ORDER = "waiver_claim_order"
    WAIVER_CLAIM_BBID = "waiver_claim_bbid"
    PROPOSE_TRADE = "propose_trade"
    RESPOND_TO_TRADE = "respond_to_trade"


@dataclass(frozen=True, slots=True)
class ReadEndpoint:
    """A documented ``export?TYPE=...`` request."""

    type_name: str
    params: tuple[str, ...]
    provenance: Provenance
    #: Suggested cache lifetime in seconds. Enforced by the client, not by
    #: caller discipline -- see :mod:`mflbot.mfl.cache`.
    ttl_seconds: int
    description: str = ""
    requires_auth: bool = False

    @property
    def is_league_scoped(self) -> bool:
        """True if this export takes a league parameter (``L``).

        Drives host selection: MFL's docs require requests with no league
        parameter to go to the ``api`` host rather than a league-specific one.
        See :attr:`mflbot.config.LeagueRef.global_base_url`.
        """
        return "L" in self.params


@dataclass(frozen=True, slots=True)
class WriteEndpoint:
    """An ``import?TYPE=...`` request. Inert until verified.

    ``type_name`` is ``None`` until ``bot verify-endpoints`` resolves it.
    ``candidates`` are names to *look for* in the live documentation. They are
    search hints only: a candidate is never used as the request TYPE unless the
    documentation confirms it exists.
    """

    capability: Capability
    candidates: tuple[str, ...]
    description: str
    type_name: str | None = None
    params: tuple[str, ...] = ()
    provenance: Provenance = Provenance.UNVERIFIED
    #: Maps a payload field name to the MFL request parameter that carries it.
    #: Resolved during verification, because knowing that an endpoint takes a
    #: parameter called ``W`` is not the same as knowing which of the payload's
    #: fields belongs in it.
    field_map: dict[str, str] = field(default_factory=dict)

    @property
    def is_verified(self) -> bool:
        return self.provenance is Provenance.DOC_VERIFIED and bool(self.type_name)

    def missing_field_mappings(self, payload_fields: tuple[str, ...]) -> tuple[str, ...]:
        """Payload fields this endpoint has no parameter mapping for."""
        return tuple(f for f in payload_fields if f not in self.field_map)

    def require_verified(self) -> str:
        if not self.is_verified:
            raise EndpointNotVerifiedError(
                f"Write capability '{self.capability}' is not verified against MFL's "
                f"API documentation, so it will not be submitted.\n"
                f"  Run:  bot verify-endpoints\n"
                f"  It reads your league's /api_info page and pins the real TYPE name "
                f"and parameters into {LOCK_FILENAME}.\n"
                f"  Until then this capability is inert by design -- the bot does not "
                f"POST guessed endpoints to your league."
            )
        assert self.type_name is not None
        return self.type_name


# ---------------------------------------------------------------------------
# Read endpoints
# ---------------------------------------------------------------------------
# TTLs encode MFL's published guidance: the full player database is fetched at
# most once a day, slow-moving league configuration a few times a day, and live
# in-game data only as often as MFL permits. Because the TTL lives here rather
# than at the call site, an analysis run that asks for the same data five times
# still generates one HTTP request.

_DAY = 86_400
_SIX_HOURS = 21_600
_HOUR = 3_600

READ_ENDPOINTS: dict[str, ReadEndpoint] = {
    e.type_name: e
    for e in (
        # -- configuration -------------------------------------------------
        ReadEndpoint("league", ("L", "FRANCHISE_ID", "PASSWORD"), Provenance.THIRD_PARTY_CLIENT,
                     _SIX_HOURS, "League settings: roster limits, lineup slots, divisions, "
                     "waiver system, trade deadline, IR/taxi rules."),
        ReadEndpoint("rules", ("L",), Provenance.THIRD_PARTY_CLIENT, _SIX_HOURS,
                     "This league's scoring rules. Sole source of truth for scoring."),
        ReadEndpoint("allRules", (), Provenance.THIRD_PARTY_CLIENT, _DAY,
                     "Catalogue of every scoring-event abbreviation MFL supports, with "
                     "descriptions. Used to resolve event codes without hardcoding them."),
        # -- player universe ----------------------------------------------
        ReadEndpoint("players", ("DETAILS", "SINCE", "PLAYERS"), Provenance.THIRD_PARTY_CLIENT,
                     _DAY, "Full player database. MFL asks that this be fetched at most "
                     "once per day."),
        ReadEndpoint("playerProfile", ("P",), Provenance.THIRD_PARTY_CLIENT, _DAY,
                     "Per-player biographical detail."),
        ReadEndpoint("playerStatus", ("L", "P"), Provenance.THIRD_PARTY_CLIENT, _HOUR,
                     "Whether a player is rostered, free agent, or on waivers."),
        # -- league state --------------------------------------------------
        ReadEndpoint("rosters", ("L", "FRANCHISE"), Provenance.THIRD_PARTY_CLIENT, _HOUR,
                     "All franchise rosters.", requires_auth=True),
        ReadEndpoint("freeAgents", ("L", "POSITION"), Provenance.THIRD_PARTY_CLIENT, _HOUR,
                     "Available player pool."),
        ReadEndpoint("transactions", ("L", "TRANS_TYPE", "FRANCHISE", "DAYS", "COUNT"),
                     Provenance.THIRD_PARTY_CLIENT, 900,
                     "League-wide transaction log; also the change-detection feed."),
        ReadEndpoint("leagueStandings", ("L",), Provenance.THIRD_PARTY_CLIENT, _HOUR,
                     "Current standings."),
        ReadEndpoint("assets", ("L",), Provenance.THIRD_PARTY_CLIENT, _HOUR,
                     "Every franchise's tradable assets (players and draft picks)."),
        ReadEndpoint("salaryAdjustments", ("L",), Provenance.THIRD_PARTY_CLIENT, _SIX_HOURS,
                     "Salary cap adjustments, where the league uses a cap."),
        ReadEndpoint("accounting", ("L",), Provenance.THIRD_PARTY_CLIENT, _SIX_HOURS,
                     "League accounting records."),
        ReadEndpoint("calendar", ("L",), Provenance.THIRD_PARTY_CLIENT, _SIX_HOURS,
                     "League calendar events."),
        # -- scoring -------------------------------------------------------
        ReadEndpoint("playerScores", ("L", "W", "PLAYERS", "COUNT", "POSITION", "STATUS"),
                     Provenance.THIRD_PARTY_CLIENT, _HOUR,
                     "Actual fantasy points under this league's scoring."),
        ReadEndpoint("weeklyResults", ("L", "W"), Provenance.THIRD_PARTY_CLIENT, _HOUR,
                     "Weekly head-to-head results."),
        ReadEndpoint("liveScoring", ("L", "W", "DETAILS"), Provenance.THIRD_PARTY_CLIENT, 300,
                     "In-progress scoring. Only polled inside game windows."),
        ReadEndpoint("projectedScores", ("L", "PLAYERS", "W", "COUNT", "POSITION", "STATUS"),
                     Provenance.THIRD_PARTY_CLIENT, _SIX_HOURS,
                     "MFL's own projections, where populated for this league's host."),
        ReadEndpoint("pointsAllowed", ("L",), Provenance.THIRD_PARTY_CLIENT, _SIX_HOURS,
                     "Points allowed by NFL defence, by position."),
        # -- context -------------------------------------------------------
        ReadEndpoint("injuries", ("W",), Provenance.THIRD_PARTY_CLIENT, 1800,
                     "Official NFL injury designations. The always-on news baseline."),
        ReadEndpoint("nflSchedule", ("W",), Provenance.THIRD_PARTY_CLIENT, _DAY,
                     "NFL game schedule and kickoff times; drives game-window polling "
                     "and late-game lock risk."),
        # -- market signal -------------------------------------------------
        ReadEndpoint("adp", ("FRANCHISES", "IS_MOCK", "IS_PPR", "IS_KEEPER", "TIME", "DAYS"),
                     Provenance.THIRD_PARTY_CLIENT, _DAY, "Average draft position."),
        ReadEndpoint("aav", ("FRANCHISES",), Provenance.THIRD_PARTY_CLIENT, _DAY,
                     "Average auction value."),
        ReadEndpoint("topAdds", ("W",), Provenance.THIRD_PARTY_CLIENT, _HOUR,
                     "Most-added players across MFL. A market interest signal."),
        ReadEndpoint("topDrops", ("W",), Provenance.THIRD_PARTY_CLIENT, _HOUR, "Most-dropped."),
        ReadEndpoint("topOwns", ("W",), Provenance.THIRD_PARTY_CLIENT, _HOUR, "Most-owned."),
        ReadEndpoint("topStarters", ("W",), Provenance.THIRD_PARTY_CLIENT, _HOUR,
                     "Most-started."),
        # -- trade context -------------------------------------------------
        ReadEndpoint("tradeBait", ("L",), Provenance.THIRD_PARTY_CLIENT, _HOUR,
                     "What each franchise has advertised as available."),
        # NOTE: pending trades are franchise-scoped and authenticated. The exact
        # TYPE name is not corroborated by an independent client, so it is left
        # UNVERIFIED and resolved by `bot verify-endpoints` alongside the writes.
        ReadEndpoint("pendingTrades", ("L", "FRANCHISE"), Provenance.UNVERIFIED, 900,
                     "Trade offers awaiting a response.", requires_auth=True),
    )
}

#: Read endpoints the bot needs but whose names are not independently
#: corroborated. Surfaced by `bot verify-endpoints` for confirmation.
UNVERIFIED_READS: tuple[str, ...] = tuple(
    name for name, e in READ_ENDPOINTS.items() if e.provenance is Provenance.UNVERIFIED
)


# ---------------------------------------------------------------------------
# Write endpoints -- all inert until verified
# ---------------------------------------------------------------------------

WRITE_ENDPOINTS: dict[Capability, WriteEndpoint] = {
    w.capability: w
    for w in (
        WriteEndpoint(
            Capability.SUBMIT_LINEUP,
            candidates=("lineup",),
            description="Submit a starting lineup for one franchise and week.",
        ),
        WriteEndpoint(
            Capability.ADD_DROP_FCFS,
            candidates=("import_transaction", "transaction", "addDrop", "freeAgent"),
            description="Immediate free-agent add and/or drop in a first-come "
                        "first-served league.",
        ),
        WriteEndpoint(
            Capability.WAIVER_CLAIM_ORDER,
            candidates=("waiverRequest", "waiverOrder", "import_transaction"),
            description="Place a waiver claim in a waiver-order league.",
        ),
        WriteEndpoint(
            Capability.WAIVER_CLAIM_BBID,
            candidates=("bbidWaiverRequest", "bbid_waiver_request", "import_transaction"),
            description="Place a blind-bid waiver claim with a bid amount.",
        ),
        WriteEndpoint(
            Capability.PROPOSE_TRADE,
            candidates=("tradeProposal", "proposeTrade", "import_transaction"),
            description="Offer a trade to another franchise.",
        ),
        WriteEndpoint(
            Capability.RESPOND_TO_TRADE,
            candidates=("tradeResponse", "respondToTrade", "import_transaction"),
            description="Accept or reject a received trade offer.",
        ),
    )
}


@dataclass(slots=True)
class EndpointRegistry:
    """Runtime view of the registry, with any verified overrides applied."""

    reads: dict[str, ReadEndpoint] = field(default_factory=lambda: dict(READ_ENDPOINTS))
    writes: dict[Capability, WriteEndpoint] = field(
        default_factory=lambda: dict(WRITE_ENDPOINTS)
    )
    lock_path: Path | None = None

    @classmethod
    def load(cls, lock_path: Path | str = LOCK_FILENAME) -> EndpointRegistry:
        """Build a registry, applying ``endpoints.lock.json`` when present."""
        registry = cls(lock_path=Path(lock_path))
        path = Path(lock_path)
        if not path.exists():
            return registry
        data = json.loads(path.read_text(encoding="utf-8"))
        for name, spec in data.get("reads", {}).items():
            registry.reads[name] = ReadEndpoint(
                type_name=name,
                params=tuple(spec.get("params", ())),
                provenance=Provenance.DOC_VERIFIED,
                ttl_seconds=int(
                    spec.get(
                        "ttl_seconds",
                        registry.reads[name].ttl_seconds if name in registry.reads else _HOUR,
                    )
                ),
                description=spec.get("description", ""),
                requires_auth=bool(spec.get("requires_auth", False)),
            )
        for cap_name, spec in data.get("writes", {}).items():
            cap = Capability(cap_name)
            base = registry.writes[cap]
            registry.writes[cap] = WriteEndpoint(
                capability=cap,
                candidates=base.candidates,
                description=base.description,
                type_name=spec["type_name"],
                params=tuple(spec.get("params", ())),
                provenance=Provenance.DOC_VERIFIED,
                field_map=dict(spec.get("field_map", {})),
            )
        return registry

    def save_lock(self, data: dict[str, Any], lock_path: Path | str | None = None) -> Path:
        path = Path(lock_path or self.lock_path or LOCK_FILENAME)
        path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path

    def read(self, type_name: str) -> ReadEndpoint:
        try:
            return self.reads[type_name]
        except KeyError:
            raise KeyError(
                f"Unknown MFL export type '{type_name}'. Add it to READ_ENDPOINTS "
                f"with its provenance before using it."
            ) from None

    def write(self, capability: Capability) -> WriteEndpoint:
        return self.writes[capability]

    def unverified_writes(self) -> tuple[Capability, ...]:
        return tuple(cap for cap, w in self.writes.items() if not w.is_verified)
