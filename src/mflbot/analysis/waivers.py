"""Add/drop and waiver-claim analysis.

Produces ranked add/drop *proposals*. It cannot execute anything: this module
imports no write client, and the objects it returns are recommendations that
still need a human decision.

The analysis blocks rather than guesses when the league's waiver system is
unknown, when the blind-bid budget is unknown in a bidding league, or when a
player has no real projection. Each of those would otherwise produce a claim
the league rejects, or a bid sized from an invented number.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ..domain.models import LeagueSettings, Player, WaiverSystem
from ..errors import BlockedFeature, Missing
from ..mfl.endpoints import Capability
from ..recommend.models import (
    AddDropPayload,
    Confidence,
    Evidence,
    Recommendation,
    RecommendationKind,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PlayerValue:
    """A player's projected value, with the coverage behind it."""

    player: Player
    next_week: float | None
    rest_of_season: float | None
    weeks_covered: int = 0

    @property
    def position(self) -> str | None:
        return self.player.position

    @property
    def has_value(self) -> bool:
        return self.next_week is not None or self.rest_of_season is not None


@dataclass(frozen=True, slots=True)
class AddDropIdea:
    add: PlayerValue
    drop: PlayerValue
    next_week_delta: float | None
    ros_delta: float | None
    trigger: str
    confidence: Confidence
    caveats: tuple[str, ...]
    bid_amount: float | None = None
    bid_rationale: str = ""


def value_players(
    players: Sequence[Player], projections, week: int, remaining_weeks: Sequence[int]
) -> list[PlayerValue]:
    """Attach projections to players, leaving None where none exist."""
    out: list[PlayerValue] = []
    for player in players:
        next_week = projections.week(week).get(player.player_id)
        ros = projections.rest_of_season(player.player_id, remaining_weeks)
        if isinstance(ros, Missing):
            ros_points, covered = None, 0
        else:
            ros_points, weeks = ros
            covered = len(weeks)
        out.append(PlayerValue(player, next_week, ros_points, covered))
    return out


def recommend_bid(
    settings: LeagueSettings,
    idea_rank: int,
    total_ideas: int,
    aggressiveness: float,
) -> tuple[float | None, str]:
    """Size a blind bid from the *real* remaining budget.

    Returns ``(None, reason)`` when the budget is unknown -- a bid guessed from
    an assumed budget is worse than no bid, because it is submitted for real
    money in a league that tracks it.
    """
    owner = settings.owner_franchise
    if owner is None:
        return None, "the user's own franchise is not identified (run `bot whoami`)"
    if owner.bbid_budget is None:
        return None, "MFL did not report a remaining blind-bid budget for this franchise"
    budget = owner.bbid_budget
    if budget <= 0:
        return 0.0, f"remaining budget is ${budget:g}; no bid is possible"

    # Spend proportionally more on the highest-ranked idea, and never commit the
    # whole budget to one claim.
    weight = 1.0 if total_ideas <= 1 else (total_ideas - idea_rank) / total_ideas
    amount = round(budget * aggressiveness * weight, 2)
    amount = max(amount, 1.0) if budget >= 1.0 else round(budget, 2)
    amount = min(amount, budget)
    return amount, (
        f"${amount:g} of ${budget:g} remaining "
        f"({aggressiveness:.0%} aggressiveness, rank {idea_rank + 1} of {total_ideas}). "
        f"This is a bid ceiling, not a valuation -- edit it before approving if you "
        f"read the room differently."
    )


def analyse_waivers(
    settings: LeagueSettings,
    roster: Sequence[Player],
    free_agents: Sequence[Player],
    projections,
    week: int,
    remaining_weeks: Sequence[int],
    waiver_settings,
    *,
    news_by_player: dict[str, list[dict]] | None = None,
) -> tuple[list[AddDropIdea], list[BlockedFeature]]:
    """Rank add/drop opportunities. Returns ideas plus any blocking conditions."""
    blocks: list[BlockedFeature] = []

    if settings.waiver_system == WaiverSystem.UNKNOWN:
        blocks.append(
            BlockedFeature(
                feature="waivers",
                reason="the league's waiver system is unknown",
                gaps=(f"waiver type reported as {settings.waiver_type_raw!r}",),
                remedy="Run `bot sync-config --force`. The claim workflow differs "
                       "between blind bidding and first-come-first-served.",
            )
        )
        return [], blocks
    if settings.waiver_system == WaiverSystem.NONE:
        return [], blocks

    roster_values = value_players(roster, projections, week, remaining_weeks)
    fa_values = value_players(free_agents, projections, week, remaining_weeks)

    projected_fa = [
        v
        for v in fa_values
        if v.has_value
        and (v.next_week or 0.0) >= waiver_settings.candidate_projection_floor
    ]
    if not projected_fa:
        unprojected = sum(1 for v in fa_values if not v.has_value)
        if unprojected == len(fa_values) and fa_values:
            blocks.append(
                BlockedFeature(
                    feature="waivers",
                    reason="no free agent has a projection, so none can be compared",
                    gaps=(f"{unprojected} free agents, zero projections",),
                    remedy="MFL publishes projections per host; if this league's host "
                           "has none, add a projection source before waiver analysis "
                           "can run.",
                )
            )
        return [], blocks

    news_by_player = news_by_player or {}
    ideas: list[AddDropIdea] = []

    for candidate in sorted(projected_fa, key=lambda v: -(v.next_week or 0.0)):
        # Only drop someone the candidate can actually replace: same position,
        # weakest first, and never a player without a projection (dropping an
        # unknown quantity is a decision the user should make, not the bot).
        droppable = [
            v
            for v in roster_values
            if v.position == candidate.position and v.next_week is not None
        ]
        if not droppable:
            continue
        weakest = min(droppable, key=lambda v: v.next_week or 0.0)

        next_delta = None
        if candidate.next_week is not None and weakest.next_week is not None:
            next_delta = round(candidate.next_week - weakest.next_week, 2)
        ros_delta = None
        if candidate.rest_of_season is not None and weakest.rest_of_season is not None:
            ros_delta = round(candidate.rest_of_season - weakest.rest_of_season, 2)

        meets_weekly = next_delta is not None and next_delta >= waiver_settings.min_projection_delta
        meets_ros = ros_delta is not None and ros_delta >= waiver_settings.min_ros_delta
        if not (meets_weekly or meets_ros):
            continue

        news = news_by_player.get(candidate.player.player_id, [])
        trigger = _describe_trigger(news, next_delta, ros_delta)
        confidence, caveats = _assess_confidence(candidate, weakest, news, next_delta)

        ideas.append(
            AddDropIdea(
                add=candidate,
                drop=weakest,
                next_week_delta=next_delta,
                ros_delta=ros_delta,
                trigger=trigger,
                confidence=confidence,
                caveats=caveats,
            )
        )
        if len(ideas) >= waiver_settings.max_recommendations:
            break

    if settings.waiver_system == WaiverSystem.BLIND_BID:
        priced: list[AddDropIdea] = []
        for rank, idea in enumerate(ideas):
            amount, rationale = recommend_bid(
                settings, rank, len(ideas), waiver_settings.bbid_aggressiveness
            )
            if amount is None:
                blocks.append(
                    BlockedFeature(
                        feature="waivers",
                        reason="this is a blind-bid league and a bid cannot be sized",
                        gaps=(rationale,),
                        remedy="No claim is prepared without a real budget figure.",
                    )
                )
                return [], blocks
            priced.append(
                AddDropIdea(
                    add=idea.add, drop=idea.drop,
                    next_week_delta=idea.next_week_delta, ros_delta=idea.ros_delta,
                    trigger=idea.trigger, confidence=idea.confidence,
                    caveats=idea.caveats, bid_amount=amount, bid_rationale=rationale,
                )
            )
        ideas = priced

    return ideas, blocks


def _describe_trigger(news: list[dict], next_delta, ros_delta) -> str:
    if news:
        headline = news[0].get("headline") or "recent news"
        source = news[0].get("source") or "unknown source"
        return f"{headline} ({source})"
    if next_delta is not None and ros_delta is not None and ros_delta > next_delta * 4:
        return "rest-of-season projection, not a single-week spike"
    return "projection gap over the weakest rostered player at this position"


def _assess_confidence(
    candidate: PlayerValue, weakest: PlayerValue, news: list[dict], next_delta
) -> tuple[Confidence, tuple[str, ...]]:
    """State the evidence honestly, including when it is thin."""
    caveats: list[str] = []
    score = 0

    if news:
        score += 1
    else:
        caveats.append(
            "No news item motivated this; it rests on projections alone."
        )
    if candidate.weeks_covered >= 4:
        score += 1
    else:
        caveats.append(
            f"Rest-of-season value covers only {candidate.weeks_covered} projected "
            f"week(s), so it is a partial picture."
        )
    if next_delta is not None and next_delta >= 3.0:
        score += 1
    elif next_delta is not None:
        caveats.append(
            f"The weekly edge is {next_delta:.1f} pts, inside the range where "
            f"projection error alone could reverse it."
        )
    if any("Trending" in (n.get("headline") or "") for n in news):
        caveats.append(
            "Part of the signal is market interest (trending adds), which reflects "
            "what other managers believe, not what has happened on the field."
        )

    confidence = Confidence.HIGH if score >= 3 else Confidence.MEDIUM if score == 2 else Confidence.LOW
    return confidence, tuple(caveats)


def build_recommendations(
    ideas: Sequence[AddDropIdea],
    settings: LeagueSettings,
    franchise_id: str,
    *,
    expires_at: datetime | None = None,
) -> list[Recommendation]:
    """Turn ideas into persistable, approvable recommendations."""
    expires_at = expires_at or (datetime.now(UTC) + timedelta(days=1))
    capability = {
        WaiverSystem.BLIND_BID: Capability.WAIVER_CLAIM_BBID,
        WaiverSystem.WAIVER_ORDER: Capability.WAIVER_CLAIM_ORDER,
        WaiverSystem.FCFS: Capability.ADD_DROP_FCFS,
    }.get(settings.waiver_system, Capability.ADD_DROP_FCFS)

    out: list[Recommendation] = []
    for idea in ideas:
        rationale_lines = [
            f"Add {idea.add.player.display}, drop {idea.drop.player.display}.",
            f"Trigger: {idea.trigger}.",
        ]
        if idea.next_week_delta is not None:
            rationale_lines.append(
                f"Next week: {idea.add.next_week:.1f} projected vs "
                f"{idea.drop.next_week:.1f} ({idea.next_week_delta:+.1f})."
            )
        if idea.ros_delta is not None:
            rationale_lines.append(
                f"Rest of season: {idea.add.rest_of_season:.1f} vs "
                f"{idea.drop.rest_of_season:.1f} ({idea.ros_delta:+.1f}) across "
                f"{idea.add.weeks_covered} projected week(s)."
            )
        if settings.waiver_system == WaiverSystem.FCFS:
            rationale_lines.append(
                "This league is first-come-first-served, so the claim is only worth "
                "making while the player is still unrostered -- timing matters more "
                "than the size of the edge."
            )
        if idea.bid_rationale:
            rationale_lines.append(f"Bid: {idea.bid_rationale}")
        rationale_lines.append(
            "Projections are estimates, not forecasts of what will happen."
        )

        payload = AddDropPayload(
            capability=capability,
            league_id=settings.league_id,
            franchise_id=franchise_id,
            add_player_id=idea.add.player.player_id,
            drop_player_id=idea.drop.player.player_id,
            bid_amount=idea.bid_amount,
            waiver_system=settings.waiver_system,
        )
        out.append(
            Recommendation(
                kind=RecommendationKind.ADD_DROP,
                payload=payload,
                rationale="\n".join(rationale_lines),
                evidence=Evidence(
                    projections={
                        idea.add.player.player_id: idea.add.next_week or 0.0,
                        idea.drop.player.player_id: idea.drop.next_week or 0.0,
                    },
                    sources=("mfl_projectedScores",),
                    notes={
                        "waiver_system": settings.waiver_system,
                        "ros_delta": idea.ros_delta,
                        "weeks_covered": idea.add.weeks_covered,
                    },
                ),
                confidence=idea.confidence,
                caveats=idea.caveats,
                expires_at=expires_at,
            )
        )
    return out
