"""Notification transport interface.

Notifications are one-way pings that say "there is something to look at". They
never carry an approval mechanism: clicking a Discord message cannot authorise
an MFL write. Approval always happens through an
:class:`~mflbot.approval.channel.ApprovalChannel`, which is a separate,
authenticated surface.
"""

from __future__ import annotations

import abc
import logging

log = logging.getLogger(__name__)


class Notifier(abc.ABC):
    transport_id: str = "unnamed"

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
