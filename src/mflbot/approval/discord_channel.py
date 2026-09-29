"""The Discord bot's :class:`~mflbot.approval.channel.ApprovalChannel`.

Decision logic is delegated to :class:`~mflbot.approval.cli_channel.CLIApprovalChannel`,
exactly as the dashboard's channel does, so the three surfaces enforce one set
of rules. What this adds is only who the actor is -- ``discord:<user id>`` on
every token and decision -- and that presentation happens as message cards
posted from the store, not as a push through this object.

Which Discord account may decide at all is checked before a call reaches this
channel, in :mod:`mflbot.discordbot.actions`.
"""

from __future__ import annotations

from collections.abc import Sequence

from ..recommend.models import Recommendation
from .channel import ApprovalChannel, ApprovalDecision
from .cli_channel import CLIApprovalChannel


def discord_actor(user_id: str) -> str:
    return f"discord:{user_id}"


class DiscordApprovalChannel(ApprovalChannel):
    channel_id = "discord"

    def __init__(self, store, tokens, notifier=None) -> None:
        self._inner = CLIApprovalChannel(store, tokens, notifier, writer=lambda _: None)

    def present(self, recommendations: Sequence[Recommendation]) -> None:
        """No-op: the bot posts cards from the store rather than being pushed to."""
        return None

    def approve(self, recommendation_id: str, *, actor: str) -> ApprovalDecision:
        return self._inner.approve(recommendation_id, actor=actor)

    def reject(
        self, recommendation_id: str, *, actor: str, note: str = ""
    ) -> ApprovalDecision:
        return self._inner.reject(recommendation_id, actor=actor, note=note)

    def edit(
        self, recommendation_id: str, changes: dict, *, actor: str
    ) -> ApprovalDecision:
        return self._inner.edit(recommendation_id, changes, actor=actor)
