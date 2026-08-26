"""View models: domain objects flattened into what a template needs.

Templates get dictionaries of already-decided values, never live objects they
could call a network method on. Two reasons, both learned from the scaffold
this replaces:

* A page render must not be able to trigger an MFL request. Everything here
  reads the database or already-loaded state; nothing calls the API.
* Every value a template shows can then be asserted in a test without a
  browser, because building it is a pure function.

Player ids are resolved to names here too. The approval screen still shows the
literal payload -- the ids that would actually be sent -- but a human deciding
whether to bench someone should not have to translate "13129" in their head.
"""

from __future__ import annotations

from dataclasses import fields as dataclass_fields
from datetime import UTC, datetime
from typing import Any

from ..approval.cli_channel import EDITABLE_FIELDS
from ..recommend.models import Recommendation, RecommendationStatus

#: Payload fields that hold one player id, or a tuple of them. Used to attach
#: readable names next to the ids on the approval screen.
_PLAYER_ID_FIELDS = frozenset(
    {"add_player_id", "drop_player_id", "starter_ids", "gives_player_ids",
     "receives_player_ids"}
)

STATUS_TONE = {
    RecommendationStatus.PROPOSED: "pending",
    RecommendationStatus.APPROVED: "approved",
    RecommendationStatus.EXECUTED: "good",
    RecommendationStatus.REJECTED: "muted",
    RecommendationStatus.EXPIRED: "muted",
    RecommendationStatus.FAILED: "bad",
}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _stamp(value: datetime | None) -> str:
    return f"{value:%Y-%m-%d %H:%M UTC}" if value else "unknown"


def humanise_delta(target: datetime | None, now: datetime | None = None) -> str:
    """"in 4h 20m" / "3m ago". Expiry is a deadline, so it reads as one."""
    if target is None:
        return "unknown"
    now = now or datetime.now(UTC)
    seconds = (target - now).total_seconds()
    past = seconds < 0
    seconds = abs(seconds)
    if seconds < 60:
        text = f"{int(seconds)}s"
    elif seconds < 3600:
        text = f"{int(seconds // 60)}m"
    elif seconds < 86400:
        text = f"{int(seconds // 3600)}h {int((seconds % 3600) // 60)}m"
    else:
        text = f"{int(seconds // 86400)}d {int((seconds % 86400) // 3600)}h"
    return f"{text} ago" if past else f"in {text}"


def stored_week(context) -> int | None:
    """The current week *as already stored*, never fetched.

    ``BotContext.current_week`` falls back to an API call. A page render must
    not make one, so this reads the cached value and reports None otherwise --
    the dashboard then says the week is unknown and offers the job that
    resolves it.
    """
    cached = context.repos.get_state("current_week")
    if cached and cached.isdigit():
        return int(cached)
    return None


def player_names(context, player_ids) -> dict[str, str]:
    ids = [pid for pid in dict.fromkeys(player_ids) if pid]
    if not ids:
        return {}
    return {pid: player.display for pid, player in context.repos.get_players(ids).items()}


# ---------------------------------------------------------------------------
# recommendations
# ---------------------------------------------------------------------------

def editable_fields(recommendation: Recommendation) -> list[dict[str, Any]]:
    """The edit form, derived from the payload class rather than hardcoded.

    The CLI's ``EDITABLE_FIELDS`` is the single definition of what may change;
    this renders exactly those, typed, so the web form cannot offer a field the
    CLI would refuse.
    """
    payload = recommendation.payload
    out: list[dict[str, Any]] = []
    for field in dataclass_fields(payload):
        if field.name not in EDITABLE_FIELDS:
            continue
        annotation = str(field.type)
        value = getattr(payload, field.name)
        if isinstance(value, tuple):
            kind, shown = "csv", ", ".join(str(v) for v in value)
        elif "int" in annotation and "str" not in annotation:
            kind, shown = "int", "" if value is None else str(value)
        elif "float" in annotation:
            kind, shown = "float", "" if value is None else f"{value:g}"
        else:
            kind, shown = "text", "" if value is None else str(value)
        out.append(
            {
                "name": field.name,
                "label": field.name.replace("_", " "),
                "kind": kind,
                "value": shown,
                "is_list": kind == "csv",
            }
        )
    return out


def payload_rows(recommendation: Recommendation, names: dict[str, str]) -> list[dict]:
    """The literal payload, one row per field, with player names alongside."""
    rows = []
    for key, value in sorted(recommendation.payload_dict.items()):
        annotations = []
        if key in _PLAYER_ID_FIELDS:
            candidates = value if isinstance(value, list | tuple) else [value]
            annotations = [names[c] for c in candidates if c in names]
        rows.append(
            {
                "key": key,
                "value": "" if value is None else str(value),
                "names": annotations,
            }
        )
    return rows


def recommendation_view(
    recommendation: Recommendation, names: dict[str, str] | None = None
) -> dict[str, Any]:
    names = names or {}
    return {
        "id": recommendation.id,
        "kind": str(recommendation.kind),
        "kind_label": str(recommendation.kind).replace("_", " ").title(),
        "status": str(recommendation.status),
        "tone": STATUS_TONE.get(recommendation.status, "muted"),
        "confidence": str(recommendation.confidence),
        "summary": recommendation.payload.describe(),
        "rationale": recommendation.rationale,
        "rationale_lines": recommendation.rationale.splitlines(),
        "caveats": list(recommendation.caveats),
        "evidence": recommendation.evidence.to_dict(),
        "projections": [
            {"player_id": pid, "name": names.get(pid, pid), "points": points}
            for pid, points in sorted(
                recommendation.evidence.projections.items(),
                key=lambda item: item[1],
                reverse=True,
            )
        ],
        "created_at": _stamp(recommendation.created_at),
        "created_at_iso": _iso(recommendation.created_at),
        "expires_at": _stamp(recommendation.expires_at),
        "expires_at_iso": _iso(recommendation.expires_at),
        "expires_in": humanise_delta(recommendation.expires_at),
        "is_expired": recommendation.is_expired,
        "is_pending": recommendation.status == RecommendationStatus.PROPOSED
        and not recommendation.is_expired,
        "is_approved": recommendation.status == RecommendationStatus.APPROVED,
        "payload_rows": payload_rows(recommendation, names),
        "payload_hash": recommendation.payload_hash,
        "capability": str(recommendation.payload.capability),
        "editable": editable_fields(recommendation),
    }


def recommendation_ids(recommendations) -> list[str]:
    """Every player id mentioned by these recommendations, for one bulk lookup."""
    ids: list[str] = []
    for recommendation in recommendations:
        for key, value in recommendation.payload_dict.items():
            if key not in _PLAYER_ID_FIELDS:
                continue
            if isinstance(value, list | tuple):
                ids.extend(str(v) for v in value)
            elif value:
                ids.append(str(value))
        ids.extend(recommendation.evidence.projections)
    return ids


# ---------------------------------------------------------------------------
# status, league, team, audit
# ---------------------------------------------------------------------------

def status_view(context, *, jobs=None, submissions_enabled: bool = True) -> dict[str, Any]:
    """Everything `bot status` prints, plus what the header needs."""
    registry = context.registry
    unverified = registry.unverified_writes()
    settings = context.league_settings()
    pending = context.store.pending()
    counts = context.db.table_counts()
    notifier_ok, notifier_reason = context.notifier.is_configured()
    blocked = context.repos.blocked_features()
    active = jobs.active() if jobs is not None else None

    return {
        "league_id": context.config.league.id,
        "season": context.config.league.season,
        "league_name": settings.name if settings else None,
        "host": context.config.league.host,
        "franchise_id": context.owner_franchise_id(),
        "franchise_name": (
            settings.franchise(context.owner_franchise_id()).name
            if settings and context.owner_franchise_id()
            and settings.franchise(context.owner_franchise_id())
            else None
        ),
        "week": stored_week(context),
        "authenticated": context.client.is_authenticated(),
        "write_capable": context.client.can_write(),
        "notifier": context.notifier.transport_id,
        "notifier_ok": notifier_ok,
        "notifier_reason": notifier_reason,
        "submissions_enabled": submissions_enabled,
        "writes_verified": len(registry.writes) - len(unverified),
        "writes_total": len(registry.writes),
        "unverified_writes": [str(c) for c in unverified],
        "blocked_features": [
            {"feature": b.feature, "reason": b.reason, "detail": b.describe()}
            for b in blocked
        ],
        "counts": counts,
        "stored_rows": sum(counts.values()),
        "pending_count": len(pending),
        "active_job": active.as_dict(include_output=False) if active else None,
        "config_synced": settings is not None,
    }


def league_view(context) -> dict[str, Any]:
    settings = context.league_settings()
    if settings is None:
        return {"synced": False}
    model = context.scoring_model()
    return {
        "synced": True,
        "league_id": settings.league_id,
        "season": settings.season,
        "name": settings.name,
        "franchise_count": settings.franchise_count,
        "roster_size": settings.roster_size,
        "starter_count": settings.starter_count,
        "taxi_squad_size": settings.taxi_squad_size,
        "injured_reserve": settings.injured_reserve,
        "waiver_system": settings.waiver_system,
        "waiver_type_raw": settings.waiver_type_raw,
        "trade_deadline": _stamp(settings.trade_deadline),
        "trade_deadline_known": settings.trade_deadline is not None,
        "lineup_deadline": _stamp(settings.lineup_deadline),
        "lineup_deadline_in": humanise_delta(settings.lineup_deadline)
        if settings.lineup_deadline
        else "unknown",
        "lineup_deadline_known": settings.lineup_deadline is not None,
        "slots": [
            {
                "name": slot.name,
                "count": str(slot.min_starters)
                if slot.min_starters == slot.max_starters
                else f"{slot.min_starters}-{slot.max_starters}",
                "eligible": list(slot.eligible_positions),
            }
            for slot in settings.lineup_slots
        ],
        "franchises": [
            {
                "id": franchise.franchise_id,
                "name": franchise.name,
                "is_owner": franchise.is_owner,
                "bbid_budget": franchise.bbid_budget,
                "waiver_order": franchise.waiver_order,
            }
            for franchise in settings.franchises
        ],
        "rules_parsed": len(model.parsed.rules),
        "rule_gaps": [gap.describe() for gap in model.parsed.gaps],
    }


def team_view(context, week: int | None = None) -> dict[str, Any]:
    """Your roster and the free-agent pool, with this week's projections.

    Read-only, and honest about absence: a player with no stored projection
    shows "--", never 0.0. A zero would look like a decision the bot made.
    """
    franchise_id = context.owner_franchise_id()
    week = week or stored_week(context)
    projections = (
        context.repos.load_projections(
            context.config.league.id, context.config.league.season, week
        )
        if week is not None
        else {}
    )

    def rows(players):
        out = []
        for player in players:
            points = projections.get(player.player_id)
            out.append(
                {
                    "player_id": player.player_id,
                    "name": player.name,
                    "position": player.position or "?",
                    "nfl_team": player.nfl_team or "?",
                    "status": player.status,
                    "projection": points,
                    "projection_display": "--" if points is None else f"{points:.1f}",
                }
            )
        out.sort(key=lambda row: (row["projection"] is None, -(row["projection"] or 0)))
        return out

    roster = rows(context.roster_players(franchise_id)) if franchise_id else []
    free_agents = rows(context.free_agent_players())
    projected = [row["projection"] for row in roster if row["projection"] is not None]
    return {
        "week": week,
        "franchise_id": franchise_id,
        "roster": roster,
        # None, not 0.0, when nothing is projected: a zero total would read as a
        # forecast of zero points rather than as an absence of data. The count
        # travels with it so a partial total is never mistaken for a full one.
        "roster_projected": round(sum(projected), 1) if projected else None,
        "projected_players": len(projected),
        "free_agents": free_agents[:50],
        "free_agent_total": len(free_agents),
        "projection_coverage": (
            round(len(projected) / len(roster), 2) if roster else 0.0
        ),
    }


def audit_view(context, limit: int = 50) -> list[dict[str, Any]]:
    return [
        {
            "at": entry["at"],
            "outcome": entry["outcome"],
            "capability": entry["capability"] or "-",
            "recommendation_id": entry["recommendation_id"] or "",
            "request": entry["request_summary"],
            "response": entry["response_summary"] or "",
            "tone": {"confirmed": "good", "submitted": "approved"}.get(
                entry["outcome"], "bad"
            ),
        }
        for entry in context.repos.audit_entries(limit=limit)
    ]
