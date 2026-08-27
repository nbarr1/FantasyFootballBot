"""The outbound "still alive" ping.

This is deliberately not a :class:`~mflbot.notify.base.Notifier`. A notifier
carries a message *to* you and can only fire while the bot is running -- which
makes it structurally unable to tell you that the bot has stopped. A dead man's
switch inverts that: the bot pings an external service on a schedule, and the
*absence* of pings is what raises the alarm. Silence becomes the signal, which
is the only way a machine that has lost power can report losing power.

Point ``MFLBOT_HEARTBEAT_URL`` at any service that alerts on a missed check-in
(healthchecks.io, Better Stack, Cronitor, or a cron job on another machine that
touches a file and complains when it goes stale).

The URL is a credential, and an unusual one: services of this kind put the
secret in the *path*, not a query parameter, so :func:`mflbot.mfl.auth.redact`
cannot scrub it after the fact. Nothing here ever logs it -- not on success, not
on failure, not in a traceback. ``tests/test_heartbeat.py`` asserts that.
"""

from __future__ import annotations

import logging
import os
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

ENV_HEARTBEAT_URL = "MFLBOT_HEARTBEAT_URL"


class DeadManPing:
    """Pings an external monitor while the bot is healthy, and only then."""

    def __init__(self, url: str | None = None, *, http=None, timeout: float = 10.0) -> None:
        self.url = url or os.environ.get(ENV_HEARTBEAT_URL) or None
        if http is not None:
            self._http = http
            self._owns_http = False
        else:
            import httpx

            self._http = httpx.Client(timeout=timeout)
            self._owns_http = True

    def is_configured(self) -> tuple[bool, str]:
        if not self.url:
            return False, (
                f"set {ENV_HEARTBEAT_URL} to a check-in URL so a dead bot is "
                f"noticed by something other than the bot"
            )
        return True, ""

    @property
    def safe_target(self) -> str:
        """The host alone, for logs and status output.

        The path carries the secret, so only the host is ever quotable.
        """
        if not self.url:
            return "(not configured)"
        host = urlsplit(self.url).netloc
        return host or "(unparseable)"

    def ping(self, message: str = "") -> bool:
        """Check in. Returns True when the monitor acknowledged.

        A failed ping is logged and swallowed: it means the monitor is
        unreachable, which is the monitor's problem to report, and must never
        take down the scheduler that was trying to prove itself alive.
        """
        configured, reason = self.is_configured()
        if not configured:
            log.debug("heartbeat ping skipped: %s", reason)
            return False
        try:
            response = self._http.post(self.url, content=message.encode("utf-8")[:1000])
        except Exception as exc:  # noqa: BLE001 - never break the scheduler
            # type(exc) only: an httpx error message embeds the request URL.
            log.warning(
                "heartbeat ping to %s failed: %s", self.safe_target, type(exc).__name__
            )
            return False
        if response.status_code >= 300:
            log.warning(
                "heartbeat ping to %s returned HTTP %s",
                self.safe_target,
                response.status_code,
            )
            return False
        return True

    def close(self) -> None:
        if self._owns_http:
            self._http.close()
