"""Pull and parse the authoritative league configuration.

This is the bot's first task on every fresh install and again on a daily
refresh. Everything downstream -- scoring, lineup slots, waiver workflow, trade
deadline -- is read from here rather than assumed.

Field names in MFL's league export could not be verified against the official
documentation from the machine that generated this code, so every field is
looked up through :class:`FieldProbe`, which tries a set of candidate keys and
**records a miss instead of substituting a value**. ``bot config-summary``
prints both what was found and what was not, so the first authenticated run
tells the user exactly how well this parser fits their league.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..analysis.rules_parser import (
    ParsedRules,
    mfl_text,
    parse_rule_definitions,
    parse_rules,
)
from ..domain.models import Franchise, LeagueSettings, LineupSlot, WaiverSystem
from ..errors import BlockedFeature, ParseError

log = logging.getLogger(__name__)

FEATURE_SCORING = "scoring"
FEATURE_WAIVERS = "waivers"
FEATURE_LINEUP = "lineup"
FEATURE_TRADES = "trades"


@dataclass(slots=True)
class FieldProbe:
    """Reads fields by trying candidate key names, remembering what it missed.

    A miss is information, not an error: it means MFL's export does not use any
    of the names this parser knows, and the user needs to be told which setting
    the bot is therefore missing.
    """

    node: dict[str, Any]
    found: dict[str, str] = field(default_factory=dict)
    missed: list[str] = field(default_factory=list)

    def text(self, label: str, *candidates: str) -> str | None:
        for key in candidates:
            if key in self.node:
                value = mfl_text(self.node[key])
                if value not in (None, ""):
                    self.found[label] = key
                    return value
        self.missed.append(f"{label} (tried: {', '.join(candidates)})")
        return None

    def integer(self, label: str, *candidates: str) -> int | None:
        raw = self.text(label, *candidates)
        if raw is None:
            return None
        try:
            return int(float(raw))
        except ValueError:
            self.missed.append(f"{label} present as {raw!r} but not a number")
            return None

    def number(self, label: str, *candidates: str) -> float | None:
        raw = self.text(label, *candidates)
        if raw is None:
            return None
        try:
            return float(raw)
        except ValueError:
            self.missed.append(f"{label} present as {raw!r} but not a number")
            return None


def normalise_waiver_system(raw: str | None) -> str:
    """Map MFL's waiver-type string onto a workflow.

    Returns :data:`WaiverSystem.UNKNOWN` for anything unrecognised. That value
    blocks the waiver feature -- the add/drop workflow differs materially
    between blind bidding and first-come-first-served, and picking one at random
    would generate claims the league will reject.
    """
    if raw is None:
        return WaiverSystem.UNKNOWN
    text = raw.strip().lower()
    if not text:
        return WaiverSystem.UNKNOWN
    if "bbid" in text or "blind" in text or "bid" in text:
        return WaiverSystem.BLIND_BID
    if "fcfs" in text or "first" in text or "free" in text:
        return WaiverSystem.FCFS
    if "order" in text or "reverse" in text or "rotat" in text:
        return WaiverSystem.WAIVER_ORDER
    if text in {"none", "no", "0", "off"}:
        return WaiverSystem.NONE
    return WaiverSystem.UNKNOWN


def _parse_epoch_or_iso(raw: str | None) -> datetime | None:
    """MFL reports times as unix epoch seconds in most exports."""
    if not raw:
        return None
    text = raw.strip()
    if text.isdigit():
        try:
            return datetime.fromtimestamp(int(text), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _parse_limit(raw: str | None) -> tuple[int, int] | None:
    """Parse a starter limit: ``"1"`` or ``"1-3"`` (a range, as flex slots use)."""
    if not raw:
        return None
    text = raw.strip()
    if "-" in text:
        low, _, high = text.partition("-")
        try:
            return int(low), int(high)
        except ValueError:
            return None
    try:
        count = int(text)
    except ValueError:
        return None
    return count, count


def parse_lineup_slots(starters_node: Any) -> tuple[tuple[LineupSlot, ...], list[str]]:
    """Parse the starting-lineup requirement into slots.

    MFL names flex slots by joining eligible positions with ``/`` -- ``RB/WR/TE``
    -- so eligibility comes straight from the slot name rather than from a
    hardcoded notion of what "flex" means in some other league.
    """
    misses: list[str] = []
    if not isinstance(starters_node, dict):
        return (), ["league export has no parseable 'starters' section"]

    positions = starters_node.get("position", [])
    if isinstance(positions, dict):
        positions = [positions]
    slots: list[LineupSlot] = []
    for index, node in enumerate(positions or []):
        if not isinstance(node, dict):
            continue
        name = mfl_text(node.get("name")) or mfl_text(node.get("position"))
        limit = _parse_limit(mfl_text(node.get("limit")))
        if not name:
            misses.append(f"starter slot #{index} has no position name")
            continue
        if limit is None:
            misses.append(f"starter slot {name!r} has no parseable limit")
            continue
        eligible = tuple(p.strip() for p in name.split("/") if p.strip())
        slots.append(
            LineupSlot(
                index=index,
                name=name,
                eligible_positions=eligible,
                min_starters=limit[0],
                max_starters=limit[1],
            )
        )
    if not slots:
        misses.append("no starting lineup slots could be parsed from the league export")
    return tuple(slots), misses


def parse_franchises(
    franchises_node: Any, owner_franchise_id: str | None
) -> tuple[Franchise, ...]:
    if not isinstance(franchises_node, dict):
        return ()
    nodes = franchises_node.get("franchise", [])
    if isinstance(nodes, dict):
        nodes = [nodes]
    out: list[Franchise] = []
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        fid = mfl_text(node.get("id"))
        if not fid:
            continue
        probe = FieldProbe(node)
        out.append(
            Franchise(
                franchise_id=fid,
                name=mfl_text(node.get("name")),
                division=mfl_text(node.get("division")),
                is_owner=(owner_franchise_id is not None and fid == owner_franchise_id),
                bbid_budget=probe.number("bbid_budget", "bbidAvailableBalance",
                                         "bbidBalance", "bbidAvailable"),
                waiver_order=probe.integer("waiver_order", "waiverOrder", "waiverSortOrder"),
            )
        )
    return tuple(out)


@dataclass(slots=True)
class ConfigSyncResult:
    settings: LeagueSettings
    parsed_rules: ParsedRules
    #: Settings this parser looked for and did not find in the export.
    missing_fields: tuple[str, ...]
    #: Features that must stay disabled given what was and was not parsed.
    blocked: tuple[BlockedFeature, ...]
    rule_definition_count: int = 0

    @property
    def is_healthy(self) -> bool:
        return not self.blocked


def parse_league_settings(
    payload: Any, league_id: str, season: int, owner_franchise_id: str | None = None
) -> tuple[LeagueSettings, list[str]]:
    root = payload.get("league") if isinstance(payload, dict) else None
    if not isinstance(root, dict):
        raise ParseError(
            "The league export did not contain a 'league' object. "
            "Confirm the endpoint with `bot verify-endpoints`."
        )

    probe = FieldProbe(root)
    name = probe.text("name", "name")
    waiver_type_raw = probe.text(
        "waiver_type", "waiverType", "waivers", "waiverSystem", "waiverRule"
    )
    roster_size = probe.integer("roster_size", "rosterSize", "rosterLimit")
    starter_count = probe.integer("starter_count", "starterCount", "startersCount")
    taxi = probe.integer("taxi_squad_size", "taxiSquad", "taxiSquadSize")
    ir = probe.integer("injured_reserve", "injuredReserve", "irSize")
    trade_deadline = _parse_epoch_or_iso(
        probe.text("trade_deadline", "tradeDeadline", "tradeEndDate")
    )
    lineup_deadline = _parse_epoch_or_iso(
        probe.text("lineup_deadline", "lineupDeadline", "startWeekDeadline",
                   "standingsSort")
    )

    slots, slot_misses = parse_lineup_slots(root.get("starters"))
    franchises_node = root.get("franchises")
    franchises = parse_franchises(franchises_node, owner_franchise_id)
    franchise_count = None
    if isinstance(franchises_node, dict):
        franchise_count = FieldProbe(franchises_node).integer("franchise_count", "count")
    if franchise_count is None and franchises:
        franchise_count = len(franchises)

    settings = LeagueSettings(
        league_id=league_id,
        season=season,
        name=name,
        franchise_count=franchise_count,
        roster_size=roster_size,
        starter_count=starter_count if starter_count is not None else (
            sum(s.min_starters for s in slots) or None
        ),
        taxi_squad_size=taxi,
        injured_reserve=ir,
        waiver_type_raw=waiver_type_raw,
        waiver_system=normalise_waiver_system(waiver_type_raw),
        trade_deadline=trade_deadline,
        lineup_deadline=lineup_deadline,
        lineup_slots=slots,
        franchises=franchises,
    )
    return settings, probe.missed + slot_misses


def evaluate_blocks(
    settings: LeagueSettings, parsed_rules: ParsedRules
) -> tuple[BlockedFeature, ...]:
    """Decide which features cannot safely run, given what was parsed."""
    blocked: list[BlockedFeature] = []

    if not parsed_rules.is_complete:
        blocked.append(
            BlockedFeature(
                feature=FEATURE_SCORING,
                reason="the league's scoring rules could not be fully parsed",
                gaps=tuple(g.describe() for g in parsed_rules.gaps),
                remedy="Report these rule forms so the parser can be extended. Until "
                       "then every scoring-dependent recommendation stays disabled -- "
                       "the bot will not substitute a standard scoring format.",
            )
        )

    if not settings.lineup_slots:
        blocked.append(
            BlockedFeature(
                feature=FEATURE_LINEUP,
                reason="no starting lineup slots were found in the league export",
                gaps=("league.starters.position was absent or unparseable",),
                remedy="Run `bot verify-endpoints`, then `bot sync-config --force`.",
            )
        )

    if settings.waiver_system == WaiverSystem.UNKNOWN:
        blocked.append(
            BlockedFeature(
                feature=FEATURE_WAIVERS,
                reason="the league's waiver system could not be determined",
                gaps=(f"waiver type reported as {settings.waiver_type_raw!r}",),
                remedy="The add/drop workflow differs between blind bidding and "
                       "first-come-first-served, so no claim can be prepared until "
                       "the system is known.",
            )
        )
    elif settings.waiver_system == WaiverSystem.BLIND_BID:
        owner = settings.owner_franchise
        if owner is not None and owner.bbid_budget is None:
            blocked.append(
                BlockedFeature(
                    feature=FEATURE_WAIVERS,
                    reason="this is a blind-bid league but the remaining budget is unknown",
                    gaps=("no bbid balance field found for the owner franchise",),
                    remedy="A bid cannot be sized without the real remaining budget.",
                )
            )

    if settings.trade_deadline is None:
        blocked.append(
            BlockedFeature(
                feature=FEATURE_TRADES,
                reason="the league's trade deadline is unknown",
                gaps=("no trade deadline field found in the league export",),
                remedy="The bot will not draft proposals it cannot confirm are still "
                       "legal to make.",
            )
        )

    return tuple(blocked)


def sync_config(client, repos, *, owner_franchise_id: str | None = None,
                force: bool = False) -> ConfigSyncResult:
    """Fetch, parse and persist league settings, scoring rules and the event
    catalogue. Safe to run daily; it is the daily refresh."""
    league_payload = client.export(
        "league", L=client.league.id, force_refresh=force
    ).payload
    settings, missing = parse_league_settings(
        league_payload,
        client.league.id,
        client.league.season,
        owner_franchise_id or client.league.franchise_id,
    )

    rules_payload = client.export("rules", L=client.league.id, force_refresh=force).payload
    parsed_rules = parse_rules(rules_payload)

    definition_count = 0
    try:
        definitions = parse_rule_definitions(
            client.export("allRules", force_refresh=force).payload
        )
        definition_count = repos.save_rule_definitions(definitions)
    except Exception as exc:  # noqa: BLE001 - catalogue is explanatory, not critical
        log.warning("Could not refresh the scoring-event catalogue: %s", exc)

    repos.save_league_settings(settings, league_payload)
    repos.save_scoring_rules(
        client.league.id, client.league.season, parsed_rules.rules, parsed_rules.gaps
    )

    blocked = evaluate_blocks(settings, parsed_rules)
    blocked_names = {b.feature for b in blocked}
    for item in blocked:
        repos.block_feature(item)
    for feature in (FEATURE_SCORING, FEATURE_LINEUP, FEATURE_WAIVERS, FEATURE_TRADES):
        if feature not in blocked_names:
            repos.unblock_feature(feature)

    return ConfigSyncResult(
        settings=settings,
        parsed_rules=parsed_rules,
        missing_fields=tuple(missing),
        blocked=blocked,
        rule_definition_count=definition_count,
    )
