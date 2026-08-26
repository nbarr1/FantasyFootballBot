"""The dashboard's :class:`~mflbot.approval.channel.ApprovalChannel`.

Decision logic is delegated to :class:`~mflbot.approval.cli_channel.CLIApprovalChannel`
rather than duplicated. Both surfaces must enforce the same rules -- one
approval per recommendation, expiry refused, edits invalidating tokens -- and
one implementation cannot drift from itself.

What this class adds over the CLI channel is only the parts that differ in a
browser: presentation is a page render rather than a push, and the actor
recorded on the token names the web session.
"""

from __future__ import annotations

from collections.abc import Sequence

from ..recommend.models import Recommendation
from .channel import ApprovalChannel, ApprovalDecision
from .cli_channel import CLIApprovalChannel


class WebApprovalChannel(ApprovalChannel):
    channel_id = "web"

    def __init__(self, store, tokens, notifier=None) -> None:
        # writer=lambda: the inner channel's print-based presentation has no
        # meaning here; the dashboard renders from the store directly.
        self._inner = CLIApprovalChannel(store, tokens, notifier, writer=lambda _: None)
        self._store = store
        self._notifier = notifier

    def present(self, recommendations: Sequence[Recommendation]) -> None:
        """No-op: the dashboard renders on request rather than being pushed to."""
        return None

    def approve(self, recommendation_id: str, *, actor: str = "web") -> ApprovalDecision:
        return self._inner.approve(recommendation_id, actor=actor)

    def reject(
        self, recommendation_id: str, *, actor: str = "web", note: str = ""
    ) -> ApprovalDecision:
        return self._inner.reject(recommendation_id, actor=actor, note=note)

    def edit(
        self, recommendation_id: str, changes: dict, *, actor: str = "web"
    ) -> ApprovalDecision:
        return self._inner.edit(recommendation_id, changes, actor=actor)

    def notify(self, message: str, *, urgent: bool = False) -> None:
        if self._notifier is not None:
            self._notifier.send("mflbot", message, urgent=urgent)
