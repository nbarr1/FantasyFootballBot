"""Which cards to post, and which posted cards are out of date.

The Discord surface renders the store, as the dashboard does: a recommendation
is not pushed to Discord when it is made, it is found here on the next pass.
That keeps the bot independent of which process or thread created the
recommendation, and it survives restarts -- every posted card is recorded in
``ingest_state`` with the message it lives in and what it showed, so a restart
neither posts a card twice nor loses track of one.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..recommend.models import Recommendation, RecommendationStatus
from .render import card_key

CARD_STATE_PREFIX = "discord:card:"

#: How far back the publisher looks. Cards for older recommendations are left
#: as they were last drawn.
LOOKBACK = 100


@dataclass(frozen=True, slots=True)
class CardRecord:
    channel_id: str
    message_id: str
    key: str

    def encode(self) -> str:
        return f"{self.channel_id}/{self.message_id}/{self.key}"

    @classmethod
    def decode(cls, raw: str) -> CardRecord | None:
        parts = raw.split("/", 2)
        if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
            return None
        return cls(*parts)


@dataclass(slots=True)
class PublishPlan:
    #: Pending recommendations with no card yet.
    to_post: list[Recommendation] = field(default_factory=list)
    #: Posted cards whose recommendation has moved on since they were drawn.
    to_refresh: list[tuple[Recommendation, CardRecord]] = field(default_factory=list)


def load_card(repos, recommendation_id: str) -> CardRecord | None:
    raw = repos.get_state(CARD_STATE_PREFIX + recommendation_id)
    return CardRecord.decode(raw) if raw else None


def record_card(repos, recommendation_id: str, record: CardRecord) -> None:
    repos.set_state(CARD_STATE_PREFIX + recommendation_id, record.encode())


def plan(store, repos, tokens, *, lookback: int = LOOKBACK) -> PublishPlan:
    """Work out this pass's posts and refreshes. Expires stale ones first, so
    an expired recommendation's card loses its buttons."""
    store.expire_stale()
    result = PublishPlan()
    for recommendation in store.all(limit=lookback):
        record = load_card(repos, recommendation.id)
        live = tokens.latest_for(recommendation.id) is not None
        if record is None:
            if (
                recommendation.status == RecommendationStatus.PROPOSED
                and not recommendation.is_expired
            ):
                result.to_post.append(recommendation)
            continue
        if record.key != card_key(recommendation, live):
            result.to_refresh.append((recommendation, record))
    # Oldest first, so cards arrive in the order the recommendations were made.
    result.to_post.reverse()
    return result
