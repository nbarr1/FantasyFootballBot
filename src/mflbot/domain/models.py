"""Typed domain objects.

Every field here is populated from the MFL API. Nothing has a "sensible
default" standing in for league data: fields that MFL has not told us about are
``None``, and the analysis engines treat ``None`` as a reason to block, not as a
reason to guess.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class Player:
    player_id: str
    name: str
    position: str | None = None
    nfl_team: str | None = None
    status: str | None = None

    @property
    def display(self) -> str:
        bits = [self.name]
        if self.position:
            bits.append(self.position)
        if self.nfl_team:
            bits.append(self.nfl_team)
        return f"{bits[0]} ({', '.join(bits[1:])})" if len(bits) > 1 else bits[0]


@dataclass(frozen=True, slots=True)
class RosterEntry:
    franchise_id: str
    player_id: str
    #: MFL roster status: ROSTER, TAXI_SQUAD, INJURED_RESERVE.
    roster_status: str | None = None

    @property
    def is_active_roster(self) -> bool:
        """Taxi and IR players do not count against the active roster and are
        not startable, so most analysis wants only these."""
        return self.roster_status in (None, "", "ROSTER")


@dataclass(frozen=True, slots=True)
class Franchise:
    franchise_id: str
    name: str | None = None
    division: str | None = None
    is_owner: bool = False
    #: Remaining blind-bid budget. None means MFL did not report one, which is
    #: not the same as zero -- bid sizing blocks rather than assuming.
    bbid_budget: float | None = None
    waiver_order: int | None = None


@dataclass(frozen=True, slots=True)
class LineupSlot:
    """One starting-lineup requirement, e.g. 2 RB, or 1 FLEX of RB/WR/TE."""

    index: int
    name: str
    eligible_positions: tuple[str, ...]
    min_starters: int
    max_starters: int

    def accepts(self, position: str | None) -> bool:
        return position is not None and position in self.eligible_positions


class WaiverSystem:
    """MFL waiver system identifiers.

    The active system is read from the league export. It materially changes the
    add/drop workflow -- a blind-bid league needs a bid amount, a first-come
    league needs timing urgency -- so an unrecognised value blocks the waiver
    feature instead of defaulting to one of these.
    """

    BLIND_BID = "bbid"
    FCFS = "fcfs"
    WAIVER_ORDER = "order"
    NONE = "none"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class LeagueSettings:
    """Parsed league configuration. The single source of truth downstream."""

    league_id: str
    season: int
    name: str | None = None
    franchise_count: int | None = None
    roster_size: int | None = None
    starter_count: int | None = None
    taxi_squad_size: int | None = None
    injured_reserve: int | None = None
    #: Raw waiver-type string exactly as MFL reported it, kept for the audit
    #: trail alongside the normalised value.
    waiver_type_raw: str | None = None
    waiver_system: str = WaiverSystem.UNKNOWN
    trade_deadline: datetime | None = None
    lineup_deadline: datetime | None = None
    lineup_slots: tuple[LineupSlot, ...] = ()
    franchises: tuple[Franchise, ...] = ()

    @property
    def owner_franchise(self) -> Franchise | None:
        return next((f for f in self.franchises if f.is_owner), None)

    def franchise(self, franchise_id: str) -> Franchise | None:
        return next((f for f in self.franchises if f.franchise_id == franchise_id), None)

    def trade_window_open(self, now: datetime | None = None) -> bool | None:
        """True/False, or None when MFL did not report a deadline.

        None is meaningful: the trade analyser refuses to draft proposals it
        cannot confirm are still legal.
        """
        if self.trade_deadline is None:
            return None
        return (now or datetime.now(UTC)) < self.trade_deadline


@dataclass(frozen=True, slots=True)
class StatLine:
    """A player's raw statistical production, keyed by MFL event code.

    Keys are MFL scoring-event abbreviations (resolved from the ``allRules``
    export, never hardcoded here); values are the count of that event.
    """

    player_id: str
    week: int
    events: dict[str, float] = field(default_factory=dict)

    def get(self, event_code: str) -> float:
        return self.events.get(event_code, 0.0)


@dataclass(frozen=True, slots=True)
class Projection:
    player_id: str
    week: int
    source: str
    points: float


@dataclass(frozen=True, slots=True)
class NewsItem:
    source: str
    external_id: str | None
    player_id: str | None
    player_name: str | None
    published_at: datetime | None
    classification: str
    headline: str
    body: str = ""
    url: str | None = None


@dataclass(frozen=True, slots=True)
class Transaction:
    transaction_id: str
    timestamp: datetime
    trans_type: str | None
    franchise_id: str | None
    raw: dict[str, Any]
