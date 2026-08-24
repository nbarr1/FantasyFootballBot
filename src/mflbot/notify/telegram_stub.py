"""Telegram transport -- interface point, not implemented.

Telegram needs a bot token from @BotFather and a chat id, neither of which this
deployment has. Implementing it is a single POST to
``https://api.telegram.org/bot<TOKEN>/sendMessage``; it is left undone rather
than written blind against an untested contract.
"""

from __future__ import annotations

import os

from .base import Notifier

ENV_TOKEN = "MFLBOT_TELEGRAM_TOKEN"
ENV_CHAT_ID = "MFLBOT_TELEGRAM_CHAT_ID"


class TelegramNotifier(Notifier):
    transport_id = "telegram"

    def is_configured(self) -> tuple[bool, str]:
        missing = [v for v in (ENV_TOKEN, ENV_CHAT_ID) if not os.environ.get(v)]
        if missing:
            return False, f"telegram transport needs {', '.join(missing)}"
        return False, (
            "the telegram transport is a stub: sendMessage is not implemented. "
            "Use the webhook transport, or implement send() in notify/telegram_stub.py."
        )

    def send(self, subject: str, body: str, *, urgent: bool = False) -> bool:
        _, reason = self.is_configured()
        raise NotImplementedError(reason)
