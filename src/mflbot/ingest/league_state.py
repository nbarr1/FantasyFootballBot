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

    def incoming_trades(self, owner_franchise_id: str | None) -> list[Transaction]:
        """New trade-related events involving the user's franchise."""
        if not owner_franchise_id:
            return []
        return [
            tx
            for tx in self.new_transactions
            if (tx.trans_type or "") in TRADE_TYPES
            and owner_franchise_id in (tx.franchise_id or "")
        ]

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
    new_shape = {
        fid: sorted(e.player_id for e in entries)
        for fid, entries in _group_by_franchise(roster_entries).items()
    }
    old_shape = {
        fid: sorted(e.player_id for e in entries)
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
