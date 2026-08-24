"""CLI approval channel -- the working default.

Commands (via ``bot``):

    bot pending              list everything awaiting a decision
    bot show <id>            full detail for one recommendation
    bot approve <id>         approve exactly that one, minting a token
    bot reject <id> [note]   reject it; no token is produced
    bot edit <id> k=v ...    change the action, then approve it separately

Two properties this implementation is careful about:

* Approving one item never touches another. There is no "approve all".
* An edit invalidates any token already issued, because the token signs the
  payload hash and the edit changes it.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Sequence

from ..errors import ApprovalError
from ..recommend.models import (
    ActionPayload,
    Recommendation,
    RecommendationStatus,
)
from ..recommend.store import RecommendationStore
from .channel import ApprovalChannel, ApprovalDecision, Decision
from .token import TokenService

log = logging.getLogger(__name__)

#: Payload fields a user may edit from the CLI. Deliberately narrow: league and
#: franchise ids are identity, not preference, and editing them would turn an
#: approved action into one aimed at a different team. ``expires_at`` is left
#: out too, but for a different reason: it is a ``datetime`` and _coerce()
#: below has no datetime-parsing branch, so accepting raw text for it would
#: silently persist the wrong type rather than a real timestamp.
EDITABLE_FIELDS = {
    "add_player_id", "drop_player_id", "bid_amount", "round", "starter_ids",
    "week", "to_franchise_id", "gives_player_ids", "receives_player_ids",
    "message", "response",
}


class CLIApprovalChannel(ApprovalChannel):
    channel_id = "cli"

    def __init__(
        self,
        store: RecommendationStore,
        tokens: TokenService,
        notifier=None,
        *,
        writer=print,
    ) -> None:
        self._store = store
        self._tokens = tokens
        self._notifier = notifier
        self._write = writer

    # -- presentation -------------------------------------------------------

    def present(self, recommendations: Sequence[Recommendation]) -> None:
        if not recommendations:
            self._write("Nothing is awaiting your decision.")
            return
        self._write(f"{len(recommendations)} recommendation(s) awaiting a decision:\n")
        for recommendation in recommendations:
            self._write(recommendation.render())
            self._write(
                f"  Decide : bot approve {recommendation.id} | "
                f"bot reject {recommendation.id} | "
                f"bot edit {recommendation.id} field=value\n"
            )
        self._write(
            "Nothing here is submitted unless you approve it. If you do nothing, "
            "these expire and no action is taken."
        )

    # -- decisions ----------------------------------------------------------

    def _load(self, recommendation_id: str) -> Recommendation:
        recommendation = self._store.get(recommendation_id)
        if recommendation is None:
            raise ApprovalError(f"No recommendation with id {recommendation_id!r}.")
        return recommendation

    def approve(self, recommendation_id: str, *, actor: str = "cli") -> ApprovalDecision:
        recommendation = self._load(recommendation_id)
        if recommendation.status != RecommendationStatus.PROPOSED:
            raise ApprovalError(
                f"Recommendation {recommendation_id} is already "
                f"{recommendation.status}; it cannot be approved again."
            )
        if recommendation.is_expired:
            raise ApprovalError(
                f"Recommendation {recommendation_id} expired at "
                f"{recommendation.expires_at:%Y-%m-%d %H:%M UTC}. Re-run the analysis "
                f"for a current recommendation."
            )
        token = self._tokens.issue(recommendation, actor)
        self._store.set_status(recommendation_id, RecommendationStatus.APPROVED)
        log.info("approved %s by %s", recommendation_id, actor)
        return ApprovalDecision(
            recommendation_id=recommendation_id,
            decision=Decision.APPROVE,
            token=token,
        )

    def reject(
        self, recommendation_id: str, *, actor: str = "cli", note: str = ""
    ) -> ApprovalDecision:
        self._load(recommendation_id)
        self._store.set_status(recommendation_id, RecommendationStatus.REJECTED)
        log.info("rejected %s by %s", recommendation_id, actor)
        return ApprovalDecision(
            recommendation_id=recommendation_id,
            decision=Decision.REJECT,
            note=note,
        )

    def edit(
        self, recommendation_id: str, changes: dict, *, actor: str = "cli"
    ) -> ApprovalDecision:
        """Apply field edits. Does **not** approve -- that is a separate act."""
        recommendation = self._load(recommendation_id)
        payload = recommendation.payload

        rejected = set(changes) - EDITABLE_FIELDS
        if rejected:
            raise ApprovalError(
                f"These fields cannot be edited: {', '.join(sorted(rejected))}. "
                f"Editable fields for this action: "
                f"{', '.join(sorted(EDITABLE_FIELDS & set(payload.to_dict())))}."
            )
        unknown = set(changes) - set(payload.to_dict())
        if unknown:
            raise ApprovalError(
                f"{payload.__class__.__name__} has no field(s): "
                f"{', '.join(sorted(unknown))}."
            )

        coerced = {
            key: _coerce(payload, key, value) for key, value in changes.items()
        }
        edited: ActionPayload = dataclasses.replace(payload, **coerced)
        self._store.replace_payload(recommendation_id, edited)
        log.info("edited %s by %s: %s", recommendation_id, actor, sorted(changes))
        return ApprovalDecision(
            recommendation_id=recommendation_id,
            decision=Decision.EDIT,
            edited_payload=edited,
            note="Edited. Any earlier approval no longer applies -- approve again to "
                 "authorise the new action.",
        )

    def notify(self, message: str, *, urgent: bool = False) -> None:
        if self._notifier is not None:
            self._notifier.send("mflbot", message, urgent=urgent)


def _coerce(payload: ActionPayload, field_name: str, value: str):
    """Convert a CLI string into the field's declared type."""
    current = getattr(payload, field_name, None)
    annotation = str(payload.__class__.__annotations__.get(field_name, ""))

    if "tuple" in annotation:
        return tuple(v.strip() for v in str(value).split(",") if v.strip())
    if "bool" in annotation:
        return str(value).strip().lower() in {"1", "true", "yes", "y", "accept"}
    if "int" in annotation and "str" not in annotation:
        return int(value)
    if "float" in annotation:
        return None if str(value).lower() in {"none", "null", ""} else float(value)
    if isinstance(current, tuple):
        return tuple(v.strip() for v in str(value).split(",") if v.strip())
    if str(value).lower() in {"none", "null"}:
        return None
    return value
