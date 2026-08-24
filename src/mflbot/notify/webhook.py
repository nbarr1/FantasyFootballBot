"""Generic webhook transport (works with Discord out of the box).

Discord's incoming-webhook API accepts ``{"content": "..."}``, which is also the
shape most generic webhook receivers tolerate. The URL is a secret: it is read
from the environment and never stored, logged, or shown in the approval
interface.
"""

from __future__ import annotations

import logging
import os

from .base import Notifier

log = logging.getLogger(__name__)

ENV_WEBHOOK_URL = "MFLBOT_WEBHOOK_URL"
#: Discord rejects messages over 2000 characters.
MAX_CONTENT = 1900


class WebhookNotifier(Notifier):
    transport_id = "webhook"

    def __init__(self, url: str | None = None, *, http=None) -> None:
        self.url = url or os.environ.get(ENV_WEBHOOK_URL) or None
        if http is not None:
            self._http = http
            self._owns_http = False
        else:
            import httpx

            self._http = httpx.Client(timeout=15.0)
            self._owns_http = True

    def is_configured(self) -> tuple[bool, str]:
        if not self.url:
            return False, f"set {ENV_WEBHOOK_URL} to a webhook URL to enable pings"
        return True, ""

    def send(self, subject: str, body: str, *, urgent: bool = False) -> bool:
        configured, reason = self.is_configured()
        if not configured:
            log.info("[webhook disabled: %s] %s", reason, subject)
            return False

        prefix = "**:rotating_light: " if urgent else "**"
        content = f"{prefix}{subject}**\n{body}"
        if len(content) > MAX_CONTENT:
            content = content[: MAX_CONTENT - 20] + "\n... (truncated)"
        try:
            response = self._http.post(self.url, json={"content": content})
        except Exception as exc:  # noqa: BLE001 - a failed ping must not stop analysis
            log.warning("Webhook delivery failed: %s", exc)
            return False
        if response.status_code >= 300:
            log.warning("Webhook returned HTTP %s", response.status_code)
            return False
        return True

    def close(self) -> None:
        if self._owns_http:
            self._http.close()
