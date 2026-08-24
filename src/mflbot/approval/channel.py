"""The :class:`ApprovalChannel` interface.

A channel is the surface where a human sees a recommendation and decides. Two
implementations ship: a CLI (the working default) and a local web dashboard
(scaffolded). Swapping between them changes no analysis or execution code.

Every channel must honour the same contract:

* Show the *literal* payload, not only a summary.
* Offer approve / reject / edit-then-approve, per item.
* Never approve anything the user did not individually decide.
* Produce approval only through :class:`~mflbot.approval.token.TokenService`.
* Never display credentials.
"""

from __future__ import annotations

import abc
import enum
from dataclasses import dataclass
from typing import Sequence

from ..recommend.models import ActionPayload, Recommendation
from .token import ApprovalToken


class Decision(enum.StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    EDIT = "edit"
    DEFER = "defer"


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    """One user decision about one recommendation."""

    recommendation_id: str
    decision: Decision
    #: Present only when the decision produced an authorisation to submit.
    token: ApprovalToken | None = None
    #: Present when the user edited the action before approving.
    edited_payload: ActionPayload | None = None
    note: str = ""

    @property
    def authorises_execution(self) -> bool:
        return self.decision == Decision.APPROVE and self.token is not None


class ApprovalChannel(abc.ABC):
    """Where recommendations are presented and decided."""

    channel_id: str = "unnamed"

    @abc.abstractmethod
    def present(self, recommendations: Sequence[Recommendation]) -> None:
        """Show pending recommendations to the user."""

    @abc.abstractmethod
    def approve(self, recommendation_id: str, *, actor: str) -> ApprovalDecision:
        """Record an approval and mint a token bound to the current payload."""

    @abc.abstractmethod
    def reject(self, recommendation_id: str, *, actor: str, note: str = "") -> ApprovalDecision:
        """Record a rejection. No token is produced."""

    @abc.abstractmethod
    def edit(
        self, recommendation_id: str, changes: dict, *, actor: str
    ) -> ApprovalDecision:
        """Apply an edit. Any previously issued token stops matching."""

    def notify(self, message: str, *, urgent: bool = False) -> None:
        """Optional outbound ping. Channels without one may ignore this."""
        return None
