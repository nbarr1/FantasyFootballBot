"""Notification transport interface.

Notifications are one-way pings that say "there is something to look at". They
never carry an approval mechanism: a notification cannot authorise an MFL
write, whatever transport sends it. Approval always happens through an
:class:`~mflbot.approval.channel.ApprovalChannel` -- the CLI, the dashboard, or
the Discord bot's buttons -- each of which authenticates the person deciding.

The Discord bot is the one place the two meet: its notifier DMs you alerts, and
its cards carry Approve and Reject buttons. The buttons are not part of the
notification. They belong to the bot's approval channel, which acts only for
the account in ``[discord] owner_user_id`` and only on the one recommendation a
button names.
"""

from __future__ import annotations

import abc
import logging

log = logging.getLogger(__name__)


class Notifier(abc.ABC):
    transport_id: str = "unnamed"
    #: True when this transport shows each recommendation itself (the Discord
    #: bot posts one card per recommendation). Announcements then skip the
    #: full recommendation text rather than repeat it.
    shows_recommendations: bool = False

    @abc.abstractmethod
    def send(self, subject: str, body: str, *, urgent: bool = False) -> bool:
        """Deliver a notification. Returns True on success."""

    def is_configured(self) -> tuple[bool, str]:
        return True, ""


class NullNotifier(Notifier):
    """Used when notifications are disabled or unconfigured.

    Logs instead of sending, so a missing webhook URL degrades to "you did not
    get a ping" rather than to a crash mid-analysis.
    """

    transport_id = "null"

    def __init__(self, reason: str = "notifications are not configured") -> None:
        self.reason = reason

    def is_configured(self) -> tuple[bool, str]:
        return False, self.reason

    def send(self, subject: str, body: str, *, urgent: bool = False) -> bool:
        log.info("[notification not sent: %s] %s -- %s", self.reason, subject, body)
        return False
