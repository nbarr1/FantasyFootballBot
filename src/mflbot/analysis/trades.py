"""Trade analysis: evaluating incoming offers and drafting outgoing proposals.

Two rules govern this module:

* **It never responds to anything.** An incoming offer produces a
  recommendation with a suggested response and the reasoning behind it. The
  response is sent only if the user approves it.
* **It stops at the deadline.** If the trade deadline has passed -- or is
  unknown -- no proposals are drafted. A proposal the league will reject wastes
  the other manager's attention and the user's credibility.

Proposal drafting looks for *complementary* imbalance: a position where the user
has surplus and the counterparty has need, and vice versa. A proposal that only
helps one side is not surfaced, because it would obviously be declined.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ..domain.models import LeagueSettings
from ..errors import BlockedFeature
from ..mfl.endpoints import Capability
from ..recommend.models import (
    Confidence,
    Evidence,
    Recommendation,
    RecommendationKind,
    TradeProposalPayload,
    TradeResponsePayload,
)
from .waivers import PlayerValue

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PositionStrength:
    position: str
    #: Rest-of-season value of the starters this franchise would actually play.
    starter_value: float
    #: Value sitting behind those starters -- the tradable surplus.
    depth_value: float
    count: int

    @property
    def surplus_score(self) -> float:
        return self.depth_value


@dataclass(frozen=True, slots=True)
class TradeIdea:
    to_franchise_id: str
    to_franchise_name: str | None
    gives: tuple[PlayerValue, ...]
    receives: tuple[PlayerValue, ...]
    our_gain: float
    their_gain: float
    pitch: str
    confidence: Confidence
    caveats: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OfferAssessment:
    offer_id: str
    from_franchise_id: str | None
    we_receive: tuple[PlayerValue, ...]
    we_give: tuple[PlayerValue, ...]
    net_value: float | None
    verdict: str  # accept | reject | counter
    reasoning: str
    confidence: Confidence
    caveats: tuple[str, ...]


def position_strengths(
    values: Sequence[PlayerValue], starters_per_position: dict[str, int]
) -> dict[str, PositionStrength]:
    """Split each position's value into what starts and what sits behind it."""
    by_position: dict[str, list[PlayerValue]] = {}
    for value in values:
        if value.position and value.rest_of_season is not None:
            by_position.setdefault(value.position, []).append(value)

    out: dict[str, PositionStrength] = {}
    for position, players in by_position.items():
        players.sort(key=lambda v: -(v.rest_of_season or 0.0))
        starters = starters_per_position.get(position, 1)
        starter_value = sum((p.rest_of_season or 0.0) for p in players[:starters])
        depth_value = sum((p.rest_of_season or 0.0) for p in players[starters:])
        out[position] = PositionStrength(position, starter_value, depth_value, len(players))
    return out


def starters_per_position(settings: LeagueSettings) -> dict[str, int]:
    """How many of each position the league actually starts.

    Flex slots contribute a fractional share to each eligible position, rounded
    up, because a flex seat is a real starting opportunity for all of them.
    """
    counts: dict[str, float] = {}
    for slot in settings.lineup_slots:
        eligible = slot.eligible_positions or ()
        if not eligible:
            continue
        share = slot.min_starters / len(eligible)
        for position in eligible:
            counts[position] = counts.get(position, 0.0) + share
    return {position: max(1, round(value)) for position, value in counts.items()}


def evaluate_offer(
    offer_id: str,
    from_franchise_id: str | None,
    we_receive: Sequence[PlayerValue],
    we_give: Sequence[PlayerValue],
    our_strengths: dict[str, PositionStrength],
    trade_settings,
) -> OfferAssessment:
    """Judge an incoming offer under this league's scoring and our roster shape."""
    caveats: list[str] = []

    receive_value = sum(v.rest_of_season for v in we_receive if v.rest_of_season is not None)
    give_value = sum(v.rest_of_season for v in we_give if v.rest_of_season is not None)
    unpriced = [
        v.player.display
        for v in list(we_receive) + list(we_give)
        if v.rest_of_season is None
    ]
    if unpriced:
        caveats.append(
            f"No rest-of-season projection exists for: {', '.join(unpriced)}. "
            f"They are excluded from the totals, so the comparison is incomplete."
        )
    if not we_receive or not we_give:
        caveats.append(
            "One side of this offer has no players attached, which usually means "
            "draft picks are involved. Picks are not valued by this bot."
        )

    net = round(receive_value - give_value, 2) if (we_receive or we_give) else None

    # Positional scarcity: giving from a position where we have no surplus is
    # worse than the raw point total suggests.
    scarcity_notes: list[str] = []
    for value in we_give:
        strength = our_strengths.get(value.position or "")
        if strength and strength.depth_value <= 0:
            scarcity_notes.append(
                f"{value.player.display} is not backed by depth at {value.position}"
            )

    if net is None:
        verdict, reasoning = "counter", "The offer could not be valued."
    elif unpriced:
        verdict = "counter"
        reasoning = (
            "Some pieces cannot be valued, so this is not a decision to make on the "
            "numbers alone."
        )
    elif net >= trade_settings.min_accept_gain and not scarcity_notes:
        verdict = "accept"
        reasoning = (
            f"Projects as a {net:+.1f} point gain over the rest of the season, above "
            f"your {trade_settings.min_accept_gain:.1f} threshold, without thinning a "
            f"position you lack depth at."
        )
    elif net >= trade_settings.min_accept_gain:
        verdict = "counter"
        reasoning = (
            f"The raw value is favourable ({net:+.1f}) but it costs depth: "
            + "; ".join(scarcity_notes)
            + "."
        )
    elif net <= -trade_settings.min_accept_gain:
        verdict = "reject"
        reasoning = f"Projects as a {net:+.1f} point loss over the rest of the season."
    else:
        verdict = "counter"
        reasoning = (
            f"Roughly even ({net:+.1f}), which is inside projection noise. There is no "
            f"clear reason to accept and no clear reason to refuse."
        )

    confidence = (
        Confidence.LOW
        if unpriced or net is None
        else Confidence.HIGH
        if abs(net) >= trade_settings.min_accept_gain * 2
        else Confidence.MEDIUM
    )
    caveats.append(
        "Rest-of-season projections drive this. They do not model playoff schedule "
        "strength or your opponents' rosters."
    )
    return OfferAssessment(
        offer_id=offer_id,
        from_franchise_id=from_franchise_id,
        we_receive=tuple(we_receive),
        we_give=tuple(we_give),
        net_value=net,
        verdict=verdict,
        reasoning=reasoning,
        confidence=confidence,
        caveats=tuple(caveats),
    )


def draft_proposals(
    settings: LeagueSettings,
    our_values: Sequence[PlayerValue],
    their_values_by_franchise: dict[str, Sequence[PlayerValue]],
    trade_settings,
    *,
    now: datetime | None = None,
) -> tuple[list[TradeIdea], list[BlockedFeature]]:
    """Draft up to ``max_proposals_per_week`` mutually beneficial offers."""
    now = now or datetime.now(UTC)
    window = settings.trade_window_open(now)
    if window is None:
        return [], [
            BlockedFeature(
                feature="trades",
                reason="the trade deadline is unknown, so proposals cannot be drafted",
                gaps=("no trade deadline in the league export",),
                remedy="Run `bot sync-config --force`; the bot will not propose trades "
                       "it cannot confirm are legal.",
            )
        ]
    if not window:
        log.info("Trade deadline has passed; no proposals drafted")
        return [], []

    slots = starters_per_position(settings)
    ours = position_strengths(our_values, slots)
    ideas: list[TradeIdea] = []

    for franchise_id, their_values in their_values_by_franchise.items():
        if franchise_id == (settings.owner_franchise.franchise_id
                            if settings.owner_franchise else None):
            continue
        theirs = position_strengths(their_values, slots)

        # Our surplus position, their need; and the mirror image.
        give_position = _best_surplus(ours, theirs)
        get_position = _best_surplus(theirs, ours)
        if not give_position or not get_position or give_position == get_position:
            continue

        give = _tradable_surplus(our_values, give_position, slots)
        get = _tradable_surplus(list(their_values), get_position, slots)
        if not give or not get:
            continue

        give_value = give.rest_of_season or 0.0
        get_value = get.rest_of_season or 0.0

        # Both sides must come out ahead on their own terms. A raw point
        # difference cannot show that -- it is zero-sum, so one side's gain is
        # always the other's loss. The mutual gain comes from positional fit
        # instead: each side judges the swap against its *own* roster shape,
        # converting bench value into a starting seat.
        our_start_gain = _starting_value_gain(
            ours, get_position, get_value, give_position, give_value
        )
        their_start_gain = _starting_value_gain(
            theirs, give_position, give_value, get_position, get_value
        )

        if our_start_gain < trade_settings.min_mutual_gain:
            continue
        if their_start_gain < trade_settings.min_mutual_gain:
            continue

        franchise = settings.franchise(franchise_id)
        pitch = (
            f"I'm deep at {give_position} and thin at {get_position}; looks like you're "
            f"the reverse. Would you do {give.player.name} for {get.player.name}? "
            f"Happy to adjust if the shape is wrong."
        )
        ideas.append(
            TradeIdea(
                to_franchise_id=franchise_id,
                to_franchise_name=franchise.name if franchise else None,
                gives=(give,),
                receives=(get,),
                our_gain=round(our_start_gain, 2),
                their_gain=round(their_start_gain, 2),
                pitch=pitch,
                confidence=Confidence.LOW if min(give.weeks_covered, get.weeks_covered) < 4
                else Confidence.MEDIUM,
                caveats=(
                    "Both sides' gains are estimates of *starting* value, derived from "
                    "rest-of-season projections and this league's lineup slots.",
                    "The other manager's view of their own roster may differ entirely "
                    "from what the projections say.",
                    f"Projection coverage: {give.weeks_covered} week(s) for "
                    f"{give.player.name}, {get.weeks_covered} for {get.player.name}.",
                ),
            )
        )

    ideas.sort(key=lambda idea: -idea.our_gain)
    return ideas[: trade_settings.max_proposals_per_week], []


def _best_surplus(
    have: dict[str, PositionStrength], need: dict[str, PositionStrength]
) -> str | None:
    """The position where ``have`` has the most depth that ``need`` lacks."""
    candidates = [
        (strength.surplus_score - need.get(position, PositionStrength(position, 0, 0, 0)).surplus_score,
         position)
        for position, strength in have.items()
        if strength.surplus_score > 0
    ]
    if not candidates:
        return None
    best = max(candidates)
    return best[1] if best[0] > 0 else None


def _tradable_surplus(
    values: Sequence[PlayerValue], position: str, slots: dict[str, int]
) -> PlayerValue | None:
    """The best bench player at ``position`` -- surplus, not a starter."""
    ranked = sorted(
        (v for v in values if v.position == position and v.rest_of_season is not None),
        key=lambda v: -(v.rest_of_season or 0.0),
    )
    starters = slots.get(position, 1)
    bench = ranked[starters:]
    return bench[0] if bench else None


def _starting_value_gain(
    strengths: dict[str, PositionStrength],
    gain_position: str,
    incoming_value: float,
    loss_position: str,
    outgoing_value: float,
) -> float:
    """How much of this swap converts bench value into starting value.

    Incoming value counts fully when it lands at a position of need. Outgoing
    value costs only what it was actually contributing -- bench depth is worth
    less than a starting seat, which is precisely why both sides can gain.
    """
    need = strengths.get(gain_position)
    surplus = strengths.get(loss_position)
    incoming_benefit = incoming_value if need is None or need.depth_value <= 0 else incoming_value * 0.5
    outgoing_cost = outgoing_value * (0.25 if surplus and surplus.depth_value > 0 else 1.0)
    return incoming_benefit - outgoing_cost


def build_proposal_recommendations(
    ideas: Sequence[TradeIdea], settings: LeagueSettings, franchise_id: str,
    *, expires_at: datetime | None = None,
) -> list[Recommendation]:
    expires_at = expires_at or (datetime.now(UTC) + timedelta(days=3))
    out: list[Recommendation] = []
    for idea in ideas:
        target = idea.to_franchise_name or idea.to_franchise_id
        rationale = "\n".join(
            [
                f"Propose to {target}: give "
                f"{', '.join(v.player.display for v in idea.gives)}, receive "
                f"{', '.join(v.player.display for v in idea.receives)}.",
                f"Estimated starting-value gain -- you: {idea.our_gain:+.1f}, "
                f"them: {idea.their_gain:+.1f}.",
                "Both sides convert bench depth into a starting seat, which is why "
                "this is worth offering rather than an obvious decline.",
                f"Suggested message (edit freely): \"{idea.pitch}\"",
            ]
        )
        out.append(
            Recommendation(
                kind=RecommendationKind.TRADE_PROPOSAL,
                payload=TradeProposalPayload(
                    capability=Capability.PROPOSE_TRADE,
                    league_id=settings.league_id,
                    franchise_id=franchise_id,
                    to_franchise_id=idea.to_franchise_id,
                    gives_player_ids=tuple(v.player.player_id for v in idea.gives),
                    receives_player_ids=tuple(v.player.player_id for v in idea.receives),
                    message=idea.pitch,
                ),
                rationale=rationale,
                evidence=Evidence(
                    projections={
                        v.player.player_id: v.rest_of_season or 0.0
                        for v in list(idea.gives) + list(idea.receives)
                    },
                    sources=("mfl_projectedScores",),
                    notes={"our_gain": idea.our_gain, "their_gain": idea.their_gain},
                ),
                confidence=idea.confidence,
                caveats=idea.caveats,
                expires_at=expires_at,
            )
        )
    return out


def build_response_recommendation(
    assessment: OfferAssessment, settings: LeagueSettings, franchise_id: str,
    *, expires_at: datetime | None = None,
) -> Recommendation | None:
    """Turn an offer assessment into an approvable accept/reject.

    A ``counter`` verdict deliberately produces **no** recommendation: there is
    no single action to approve, and the bot will not invent counter-terms and
    present them as analysis. The assessment is still surfaced to the user.
    """
    if assessment.verdict == "counter":
        return None
    expires_at = expires_at or (datetime.now(UTC) + timedelta(days=1))
    return Recommendation(
        kind=RecommendationKind.TRADE_RESPONSE,
        payload=TradeResponsePayload(
            capability=Capability.RESPOND_TO_TRADE,
            league_id=settings.league_id,
            franchise_id=franchise_id,
            offer_id=assessment.offer_id,
            accept=(assessment.verdict == "accept"),
        ),
        rationale="\n".join(
            [
                f"Offer {assessment.offer_id} from franchise "
                f"{assessment.from_franchise_id or 'unknown'}.",
                f"You receive: {', '.join(v.player.display for v in assessment.we_receive) or 'nothing'}.",
                f"You give: {', '.join(v.player.display for v in assessment.we_give) or 'nothing'}.",
                f"Recommendation: {assessment.verdict.upper()}. {assessment.reasoning}",
            ]
        ),
        evidence=Evidence(
            projections={
                v.player.player_id: v.rest_of_season or 0.0
                for v in list(assessment.we_receive) + list(assessment.we_give)
            },
            sources=("mfl_projectedScores",),
            notes={"net_value": assessment.net_value, "verdict": assessment.verdict},
        ),
        confidence=assessment.confidence,
        caveats=assessment.caveats,
        expires_at=expires_at,
    )
