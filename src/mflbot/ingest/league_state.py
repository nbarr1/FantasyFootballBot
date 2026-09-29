"""League-state polling and change detection.

The transaction log is the bot's primary trigger. Diffing it against what has
already been seen turns "somebody did something" into a concrete event that can
re-run analysis, without polling any endpoint harder than MFL permits.

Nothing here decides *what to do* about a change -- it only records that the
change happened and hands the new events to the caller.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..analysis.rules_parser import mfl_text
from ..domain.models import RosterEntry, Transaction
from ..errors import ParseError

log = logging.getLogger(__name__)

#: Transaction type strings MFL uses. Sourced from MFL's documented
#: TRANS_TYPE filter values; anything unrecognised is still stored, just not
#: specially classified.
TRADE_TYPES = frozenset({"TRADE", "trade"})
WAIVER_TYPES = frozenset({"WAIVER", "waiver", "BBID_WAIVER", "bbid_waiver"})
ADD_DROP_TYPES = frozenset({"FREE_AGENT", "free_agent", "ADD", "DROP", "ADD_DROP"})


def parse_rosters(payload: Any) -> list[RosterEntry]:
    root = payload.get("rosters") if isinstance(payload, dict) else None
    if not isinstance(root, dict):
        raise ParseError("The rosters export did not contain a 'rosters' object")
    franchises = root.get("franchise", [])
    if isinstance(franchises, dict):
        franchises = [franchises]
    entries: list[RosterEntry] = []
    for franchise in franchises or []:
        if not isinstance(franchise, dict):
            continue
        fid = mfl_text(franchise.get("id"))
        if not fid:
            continue
        players = franchise.get("player", [])
        if isinstance(players, dict):
            players = [players]
        for node in players or []:
            if not isinstance(node, dict):
                continue
            pid = mfl_text(node.get("id"))
            if not pid:
                continue
            entries.append(
                RosterEntry(
                    franchise_id=fid,
                    player_id=pid,
                    roster_status=mfl_text(node.get("status")),
                )
            )
    return entries


def parse_free_agents(payload: Any) -> list[str]:
    root = payload.get("freeAgents") if isinstance(payload, dict) else None
    if not isinstance(root, dict):
        raise ParseError("The freeAgents export did not contain a 'freeAgents' object")
    league_unit = root.get("leagueUnit", root)
    if isinstance(league_unit, list):
        league_unit = league_unit[0] if league_unit else {}
    nodes = league_unit.get("player", []) if isinstance(league_unit, dict) else []
    if isinstance(nodes, dict):
        nodes = [nodes]
    out = []
    for node in nodes or []:
        if isinstance(node, dict):
            pid = mfl_text(node.get("id"))
            if pid:
                out.append(pid)
    return out


def parse_transactions(payload: Any) -> list[Transaction]:
    root = payload.get("transactions") if isinstance(payload, dict) else None
    if not isinstance(root, dict):
        raise ParseError("The transactions export did not contain a 'transactions' object")
    nodes = root.get("transaction", [])
    if isinstance(nodes, dict):
        nodes = [nodes]
    out: list[Transaction] = []
    for index, node in enumerate(nodes or []):
        if not isinstance(node, dict):
            continue
        timestamp_raw = mfl_text(node.get("timestamp"))
        timestamp = _epoch(timestamp_raw) or datetime.now(UTC)
        trans_type = mfl_text(node.get("type"))
        franchise_id = mfl_text(node.get("franchise"))
        # MFL does not publish a stable transaction id, so identity is the
        # tuple that actually distinguishes one entry from another.
        transaction_id = "|".join(
            [
                timestamp_raw or str(index),
                trans_type or "?",
                franchise_id or "?",
                mfl_text(node.get("transaction")) or "",
            ]
        )[:200]
        out.append(
            Transaction(
                transaction_id=transaction_id,
                timestamp=timestamp,
                trans_type=trans_type,
                franchise_id=franchise_id,
                raw=node,
            )
        )
    return out


def _epoch(raw: str | None) -> datetime | None:
    if not raw or not raw.strip().isdigit():
        return None
    try:
        return datetime.fromtimestamp(int(raw), tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


@dataclass(slots=True)
class LeagueStateDiff:
    """What changed since the previous poll."""

    new_transactions: list[Transaction] = field(default_factory=list)
    roster_changed: bool = False
    free_agents_changed: bool = False
    added_free_agents: list[str] = field(default_factory=list)
    removed_free_agents: list[str] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(
            self.new_transactions or self.roster_changed or self.free_agents_changed
        )

    def summary(self) -> str:
        bits = []
        if self.new_transactions:
            bits.append(f"{len(self.new_transactions)} new transaction(s)")
        if self.roster_changed:
            bits.append("rosters changed")
        if self.free_agents_changed:
            bits.append(
                f"free agents changed (+{len(self.added_free_agents)}/"
                f"-{len(self.removed_free_agents)})"
            )
        return "; ".join(bits) if bits else "no changes"


# ---------------------------------------------------------------------------
# Pending trade offers
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class PendingTrade:
    """One offer awaiting a response, as the ``pendingTrades`` export lists it.

    ``gives`` and ``receives`` are from the *offering* franchise's side, the
    same way round as the ``WILL_GIVE_UP`` / ``WILL_RECEIVE`` parameters of the
    ``tradeProposal`` import that creates an offer.
    """

    trade_id: str
    offering_franchise: str
    offered_to: str
    gives: tuple[str, ...]
    receives: tuple[str, ...]
    expires: datetime | None = None
    comments: str = ""

    def assets_for(self, franchise_id: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """``(receive, give)`` from ``franchise_id``'s point of view."""
        if franchise_id == self.offered_to:
            return self.gives, self.receives
        return self.receives, self.gives


def _asset_list(raw: str | None) -> tuple[str, ...]:
    return tuple(part.strip() for part in (raw or "").split(",") if part.strip())


def parse_pending_trades(payload: Any) -> tuple[list[PendingTrade], list[str]]:
    """Parse ``pendingTrades`` into offers, plus a note for each it could not read.

    An offer is only returned when its id, both franchises and both asset lists
    were found. A partial one is reported instead: a response recommendation
    built from half an offer -- say, with the sides the wrong way round --
    would be confidently wrong advice.
    """
    root = payload.get("pendingTrades") if isinstance(payload, dict) else None
    if not isinstance(root, dict):
        raise ParseError("The pendingTrades export did not contain a 'pendingTrades' object")
    nodes = root.get("pendingTrade", [])
    if isinstance(nodes, dict):
        nodes = [nodes]
    offers: list[PendingTrade] = []
    problems: list[str] = []
    for index, node in enumerate(nodes or []):
        if not isinstance(node, dict):
            continue
        trade_id = mfl_text(node.get("trade_id")) or mfl_text(node.get("id"))
        offering = mfl_text(node.get("offeringteam")) or mfl_text(node.get("offeringTeam"))
        offered_to = mfl_text(node.get("offeredto")) or mfl_text(node.get("offeredTo"))
        gives_raw = node.get("will_give_up")
        receives_raw = node.get("will_receive")
        missing = [
            name
            for name, value in (
                ("trade_id", trade_id),
                ("offeringteam", offering),
                ("offeredto", offered_to),
                ("will_give_up", gives_raw),
                ("will_receive", receives_raw),
            )
            if value is None
        ]
        if missing:
            problems.append(
                f"pending trade #{index} is missing {', '.join(missing)}; not evaluated"
            )
            continue
        offers.append(
            PendingTrade(
                trade_id=trade_id,
                offering_franchise=offering,
                offered_to=offered_to,
                gives=_asset_list(mfl_text(gives_raw)),
                receives=_asset_list(mfl_text(receives_raw)),
                expires=_epoch(mfl_text(node.get("expires"))),
                comments=mfl_text(node.get("comments")) or "",
            )
        )
    return offers, problems


# ---------------------------------------------------------------------------
# The submitted lineup
# ---------------------------------------------------------------------------

def submitted_starters(payload: Any, franchise_id: str) -> list[str]:
    """Starters marked for ``franchise_id`` in a ``weeklyResults`` payload.

    ``weeklyResults`` covers every matchup in the league, so only the node
    whose id is this franchise (and which lists players) is read. No such node
    means an empty list, never another franchise's lineup.
    """

    def find(node: Any) -> dict | None:
        if isinstance(node, dict):
            if mfl_text(node.get("id")) == franchise_id and "player" in node:
                return node
            children = node.values()
        elif isinstance(node, list):
            children = node
        else:
            return None
        for child in children:
            found = find(child)
            if found is not None:
                return found
        return None

    def starters(node: Any) -> list[str]:
        found: list[str] = []
        if isinstance(node, dict):
            pid = mfl_text(node.get("id"))
            if pid and mfl_text(node.get("status")) == "starter":
                found.append(pid)
            for value in node.values():
                found.extend(starters(value))
        elif isinstance(node, list):
            for value in node:
                found.extend(starters(value))
        return found

    franchise = find(payload)
    return starters(franchise.get("player")) if franchise is not None else []


def poll_league_state(client, repos, *, force: bool = False) -> LeagueStateDiff:
    """Poll transactions, rosters and free agents, and diff against last-seen."""
    league_id, season = client.league.id, client.league.season
    diff = LeagueStateDiff()

    transactions = parse_transactions(
        client.export("transactions", L=league_id, force_refresh=force).payload
    )
    diff.new_transactions = repos.save_transactions(league_id, season, transactions)

    previous_rosters = repos.current_rosters(league_id, season)
    roster_entries = parse_rosters(
        client.export("rosters", L=league_id, force_refresh=force).payload
    )
    # Status is part of the shape: moving a player to IR or the taxi squad
    # changes who can start without changing who is rostered.
    new_shape = {
        fid: sorted((e.player_id, e.roster_status or "") for e in entries)
        for fid, entries in _group_by_franchise(roster_entries).items()
    }
    old_shape = {
        fid: sorted((e.player_id, e.roster_status or "") for e in entries)
        for fid, entries in previous_rosters.items()
    }
    if new_shape != old_shape:
        diff.roster_changed = bool(old_shape)  # first ever snapshot is not a "change"
        repos.save_roster_snapshot(league_id, season, roster_entries)
    elif not old_shape:
        repos.save_roster_snapshot(league_id, season, roster_entries)

    previous_fa = set(repos.current_free_agents(league_id, season))
    current_fa = set(
        parse_free_agents(client.export("freeAgents", L=league_id, force_refresh=force).payload)
    )
    if current_fa != previous_fa:
        diff.free_agents_changed = bool(previous_fa)
        diff.added_free_agents = sorted(current_fa - previous_fa)
        diff.removed_free_agents = sorted(previous_fa - current_fa)
        repos.save_free_agents(league_id, season, current_fa)
    elif not previous_fa:
        repos.save_free_agents(league_id, season, current_fa)

    repos.set_state("last_state_poll", datetime.now(UTC).isoformat(timespec="seconds"))
    log.info("League state poll: %s", diff.summary())
    return diff


def _group_by_franchise(entries: list[RosterEntry]) -> dict[str, list[RosterEntry]]:
    out: dict[str, list[RosterEntry]] = {}
    for entry in entries:
        out.setdefault(entry.franchise_id, []).append(entry)
    return out
