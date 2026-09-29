"""CLI approval channel -- the working default.

Commands (via ``bot``):

    bot pending              list everything awaiting a decision
    bot show <id>            full detail for one recommendation
    bot approve <id>         approve exactly that one, minting a token
    bot reject <id> [note]   reject it; no token is produced
    bot edit <id> k=v ...    change the action, then approve it separately

Properties this implementation is careful about:

* Approving one item never touches another. There is no "approve all".
* An edit invalidates any token already issued: the token signs the payload
  hash and the edit changes it, and the edit also revokes the token outright
  and returns the recommendation to ``proposed`` for a fresh decision.
* Rejecting withdraws any approval already given. A token minted before the
  rejection is revoked, so nothing can spend it afterwards.
* Decisions are only taken on live recommendations. An executed, failed,
  rejected or expired one is a record, not something to re-decide.
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
        with self._store.db.transaction():
            recommendation = self._load(recommendation_id)
            if recommendation.status == RecommendationStatus.APPROVED:
                # Approving again is allowed only once the earlier approval can
                # no longer be spent -- its token expired before submission.
                # While one is live, a second would be a second authorisation
                # for the same action.
                if self._tokens.latest_for(recommendation_id) is not None:
                    raise ApprovalError(
                        f"Recommendation {recommendation_id} is already approved and "
                        f"that approval is still live; it cannot be approved again."
                    )
            elif recommendation.status != RecommendationStatus.PROPOSED:
                raise ApprovalError(
                    f"Recommendation {recommendation_id} is already "
                    f"{recommendation.status}; it cannot be approved again."
                )
            if recommendation.is_expired:
                raise ApprovalError(
                    f"Recommendation {recommendation_id} expired at "
                    f"{recommendation.expires_at:%Y-%m-%d %H:%M UTC}. Re-run the "
                    f"analysis for a current recommendation."
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
        """Reject a live recommendation, withdrawing any approval already given."""
        with self._store.db.transaction():
            recommendation = self._load(recommendation_id)
            _require_live(recommendation, "rejected")
            revoked = self._tokens.revoke_for(recommendation_id, f"rejected by {actor}")
            self._store.set_status(recommendation_id, RecommendationStatus.REJECTED)
        log.info(
            "rejected %s by %s%s", recommendation_id, actor,
            f" (revoked {revoked} approval)" if revoked else "",
        )
        return ApprovalDecision(
            recommendation_id=recommendation_id,
            decision=Decision.REJECT,
            note=note,
        )

    def edit(
        self, recommendation_id: str, changes: dict, *, actor: str = "cli"
    ) -> ApprovalDecision:
        """Apply field edits. Does **not** approve -- that is a separate act.

        Editing an approved recommendation withdraws the approval: its token is
        revoked and the recommendation goes back to ``proposed``, so the edited
        action needs (and can receive) a fresh approval.
        """
        recommendation = self._load(recommendation_id)
        _require_live(recommendation, "edited")
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
        with self._store.db.transaction():
            # Re-checked inside the transaction, so a concurrent decision on the
            # same recommendation cannot slip in between the check and the write.
            _require_live(self._load(recommendation_id), "edited")
            self._tokens.revoke_for(recommendation_id, f"payload edited by {actor}")
            self._store.replace_payload(recommendation_id, edited)
            self._store.set_status(recommendation_id, RecommendationStatus.PROPOSED)
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


#: Statuses a user may still decide on. Everything else is a record.
_LIVE_STATUSES = frozenset({RecommendationStatus.PROPOSED, RecommendationStatus.APPROVED})


def _require_live(recommendation: Recommendation, verb: str) -> None:
    if recommendation.status not in _LIVE_STATUSES:
        raise ApprovalError(
            f"Recommendation {recommendation.id} is {recommendation.status}; it "
            f"cannot be {verb}. Re-run the analysis if you want a current one."
        )


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
