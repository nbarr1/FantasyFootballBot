"""Discord direct messages, through the bot client.

This transport does not talk to Discord itself. ``send`` is called from the
scheduler's threads and the dashboard's, while the Discord client lives on its
own event loop, so ``send`` only puts the message on a bounded queue. The
client (:mod:`mflbot.discordbot.client`) drains that queue and DMs the owner.

In a process with no Discord client -- a one-off `bot analyse`, say -- nothing
would ever drain the queue, so the message is logged instead and ``send``
reports it as not delivered. Recommendations are unaffected either way: the
client posts those from the database, not from this queue.
"""

from __future__ import annotations

import logging
import queue
from dataclasses import dataclass

from .base import Notifier

log = logging.getLogger(__name__)

#: Messages held for the client. Oldest are dropped first when it is full, so a
#: long disconnection cannot grow memory without bound.
OUTBOX_SIZE = 50


@dataclass(frozen=True, slots=True)
class OutgoingMessage:
    subject: str
    body: str
    urgent: bool = False


class DiscordNotifier(Notifier):
    transport_id = "discord"
    shows_recommendations = True

    def __init__(self, maxsize: int = OUTBOX_SIZE) -> None:
        self.outbox: queue.Queue[OutgoingMessage] = queue.Queue(maxsize=maxsize)
        #: Set by the Discord client when it starts draining the outbox.
        self.attached = False
        self.dropped = 0

    def send(self, subject: str, body: str, *, urgent: bool = False) -> bool:
        if not self.attached:
            log.info("[Discord bot not running in this process] %s -- %s", subject, body)
            return False
        message = OutgoingMessage(subject, body, urgent)
        while True:
            try:
                self.outbox.put_nowait(message)
                return True
            except queue.Full:
                try:
                    self.outbox.get_nowait()
                    self.dropped += 1
                    log.warning("Discord outbox full; dropped the oldest message")
                except queue.Empty:  # pragma: no cover - drained concurrently
                    pass

    def drain(self, limit: int = 20) -> list[OutgoingMessage]:
        """Take up to ``limit`` queued messages, without blocking."""
        out: list[OutgoingMessage] = []
        while len(out) < limit:
            try:
                out.append(self.outbox.get_nowait())
            except queue.Empty:
                break
        return out
