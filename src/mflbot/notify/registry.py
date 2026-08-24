"""Selects the configured notification transport."""

from __future__ import annotations

import logging

from .base import Notifier, NullNotifier
from .email_stub import EmailNotifier
from .telegram_stub import TelegramNotifier
from .webhook import WebhookNotifier

log = logging.getLogger(__name__)

TRANSPORTS = {
    WebhookNotifier.transport_id: WebhookNotifier,
    EmailNotifier.transport_id: EmailNotifier,
    TelegramNotifier.transport_id: TelegramNotifier,
}


def build_notifier(settings) -> Notifier:
    """Build the transport named in config, falling back to a no-op notifier.

    A misconfigured notifier is never fatal: the bot still produces and stores
    recommendations, the user just has to run `bot pending` to see them.
    """
    if not settings.enabled:
        return NullNotifier("notifications are disabled in config.toml")

    transport_cls = TRANSPORTS.get(settings.transport)
    if transport_cls is None:
        return NullNotifier(
            f"unknown transport {settings.transport!r}; known: "
            f"{', '.join(sorted(TRANSPORTS))}"
        )
    notifier = transport_cls()
    configured, reason = notifier.is_configured()
    if not configured:
        return NullNotifier(reason)
    return notifier
