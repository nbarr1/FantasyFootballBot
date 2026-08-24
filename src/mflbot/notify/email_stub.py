"""Email transport -- interface point, not implemented.

Sending mail reliably needs an SMTP relay or a transactional provider, plus
credentials and a from-address the user controls. None of that can be chosen
sensibly on the user's behalf, so this stub says so rather than half-working.

To implement: fill in ``send`` using ``smtplib`` with
``MFLBOT_SMTP_HOST`` / ``MFLBOT_SMTP_USER`` / ``MFLBOT_SMTP_PASSWORD`` /
``MFLBOT_EMAIL_TO``, and register it in :mod:`mflbot.notify.registry`.
"""

from __future__ import annotations

import os

from .base import Notifier

ENV_SMTP_HOST = "MFLBOT_SMTP_HOST"
ENV_EMAIL_TO = "MFLBOT_EMAIL_TO"


class EmailNotifier(Notifier):
    transport_id = "email"

    def is_configured(self) -> tuple[bool, str]:
        missing = [v for v in (ENV_SMTP_HOST, ENV_EMAIL_TO) if not os.environ.get(v)]
        if missing:
            return False, f"email transport needs {', '.join(missing)}"
        return False, (
            "the email transport is a stub: the SMTP send path is not implemented. "
            "Use the webhook transport, or implement send() in notify/email_stub.py."
        )

    def send(self, subject: str, body: str, *, urgent: bool = False) -> bool:
        _, reason = self.is_configured()
        raise NotImplementedError(reason)
