"""Authentication, sessions and CSRF for the dashboard.

The dashboard can mint approval tokens, and an approval token is the one thing
that authorises a write to MFL. "Bound to localhost" is not an access control:
any other process on the machine, any container sharing the network namespace,
and anything reachable through an SSH port-forward can talk to a localhost
port. So the dashboard authenticates.

Three mechanisms, all deliberately boring:

* **A shared secret to get in.** Either a password (``MFLBOT_WEB_PASSWORD``,
  stored only as a scrypt hash in memory) or, when none is set, an access token
  generated at startup and printed once as a login URL. There is no default
  password and no anonymous mode -- :func:`WebSecurity.create` cannot produce an
  instance that lets everyone in.
* **Server-side sessions.** The cookie carries an opaque random id and nothing
  else; every fact about the session lives in this process. Logging out or
  restarting the server really does end the session.
* **CSRF tokens on every mutating request.** A per-session token that must be
  submitted with the form, plus an Origin check when the browser sends one.
  Without this, any page you visit while logged in could POST an approval.

Failed logins are throttled, because a local password that can be guessed at
machine speed is not a password.
"""

from __future__ import annotations

import hmac
import logging
import secrets
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import scrypt
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

#: Cookie name. Prefixed to make it obvious in a browser inspector what it is.
SESSION_COOKIE = "mflbot_session"
CSRF_FIELD = "csrf_token"
CSRF_HEADER = "x-csrf-token"

#: scrypt parameters. A single verification costs ~16MB and ~50ms, which is
#: irrelevant for one login and expensive for a guesser. ``maxmem`` has to be
#: raised explicitly: OpenSSL's default ceiling is below what these need.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_MAXMEM = 64 * 1024 * 1024


class WebAuthError(Exception):
    """Login refused. The message is safe to show to whoever tried."""


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """Hash a password for in-memory comparison.

    The hash is never persisted: the dashboard reads the password from the
    environment on every start. This exists so that a heap dump or a traceback
    rendering the security object does not contain the password itself.
    """
    salt = salt or secrets.token_bytes(16)
    digest = scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        maxmem=_SCRYPT_MAXMEM,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt_hex, digest_hex = encoded.split("$")
    except ValueError:
        return False
    if algorithm != "scrypt":
        return False
    candidate = scrypt(
        password.encode("utf-8"),
        salt=bytes.fromhex(salt_hex),
        n=int(n),
        r=int(r),
        p=int(p),
        maxmem=_SCRYPT_MAXMEM,
    )
    return hmac.compare_digest(candidate.hex(), digest_hex)


@dataclass(slots=True)
class Session:
    id: str
    csrf_token: str
    actor: str
    created_at: datetime
    last_seen: datetime

    def age(self, now: datetime | None = None) -> timedelta:
        return (now or datetime.now(UTC)) - self.created_at


@dataclass(slots=True)
class WebSecurity:
    """Login policy and the live session table.

    One instance per server process. Thread-safe: the SSE stream, the job
    worker and request handlers all touch it from different threads.
    """

    #: Exactly one of these is set. Both being None is rejected in __post_init__
    #: rather than treated as "no authentication required".
    password_hash: str | None = None
    access_token: str | None = None
    session_ttl: timedelta = timedelta(hours=12)
    idle_timeout: timedelta = timedelta(hours=2)
    max_failures: int = 10
    failure_window: timedelta = timedelta(minutes=15)
    _sessions: dict[str, Session] = field(default_factory=dict)
    _failures: list[datetime] = field(default_factory=list)
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def __post_init__(self) -> None:
        if not self.password_hash and not self.access_token:
            raise ValueError(
                "WebSecurity needs a password or an access token. There is no "
                "unauthenticated mode: anything that can reach the port would be "
                "able to approve MFL writes."
            )

    @classmethod
    def create(
        cls, password: str | None = None, *, session_ttl_minutes: int = 720, **kwargs
    ) -> WebSecurity:
        """Build from a password, or generate an access token when none is set."""
        if password:
            return cls(
                password_hash=hash_password(password),
                session_ttl=timedelta(minutes=session_ttl_minutes),
                **kwargs,
            )
        return cls(
            access_token=secrets.token_urlsafe(32),
            session_ttl=timedelta(minutes=session_ttl_minutes),
            **kwargs,
        )

    # -- login -------------------------------------------------------------

    @property
    def mode(self) -> str:
        return "password" if self.password_hash else "token"

    def login_url(self, host: str, port: int) -> str:
        """The URL to open, including the access token when there is one."""
        shown = "127.0.0.1" if host in {"0.0.0.0", "::", ""} else host  # noqa: S104
        base = f"http://{shown}:{port}/"
        if self.access_token:
            return f"{base}login?token={self.access_token}"
        return base

    def _throttled(self, now: datetime) -> timedelta | None:
        self._failures = [f for f in self._failures if now - f < self.failure_window]
        if len(self._failures) < self.max_failures:
            return None
        oldest = min(self._failures)
        return self.failure_window - (now - oldest)

    def login(self, secret: str, *, actor: str = "web") -> Session:
        """Exchange the shared secret for a session. Raises on refusal."""
        now = datetime.now(UTC)
        with self._lock:
            wait = self._throttled(now)
            if wait is not None:
                raise WebAuthError(
                    f"Too many failed attempts. Try again in "
                    f"{int(wait.total_seconds()) // 60 + 1} minute(s)."
                )
            ok = (
                verify_password(secret, self.password_hash)
                if self.password_hash
                else hmac.compare_digest(secret or "", self.access_token or "")
            )
            if not ok:
                self._failures.append(now)
                log.warning("dashboard login refused (%s auth)", self.mode)
                raise WebAuthError("That is not the right password." if self.password_hash
                                   else "That access token is not valid.")
            self._failures.clear()
            session = Session(
                id=secrets.token_urlsafe(32),
                csrf_token=secrets.token_urlsafe(32),
                actor=actor,
                created_at=now,
                last_seen=now,
            )
            self._sessions[session.id] = session
            log.info("dashboard session opened for %s", actor)
            return session

    # -- session lifecycle -------------------------------------------------

    def session_for(self, session_id: str | None) -> Session | None:
        """Return the live session for a cookie value, refreshing its activity.

        Expiry is enforced here rather than by the cookie's own max-age, which
        a client controls and can simply not honour.
        """
        if not session_id:
            return None
        now = datetime.now(UTC)
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            if now - session.created_at > self.session_ttl or (
                self.idle_timeout is not None
                and now - session.last_seen > self.idle_timeout
            ):
                del self._sessions[session.id]
                return None
            session.last_seen = now
            return session

    def logout(self, session_id: str | None) -> None:
        if not session_id:
            return
        with self._lock:
            self._sessions.pop(session_id, None)

    def session_count(self) -> int:
        with self._lock:
            return len(self._sessions)

    # -- CSRF --------------------------------------------------------------

    def check_csrf(self, session: Session, submitted: str | None) -> bool:
        return bool(submitted) and hmac.compare_digest(session.csrf_token, submitted)


def origin_is_allowed(origin: str | None, host_header: str | None) -> bool:
    """Reject a cross-site POST that a browser was honest enough to label.

    A missing Origin is allowed: non-browser clients (curl, the tests) do not
    send one, and the CSRF token is the real defence. A *present* Origin that
    disagrees with the Host is a cross-site request, and no legitimate form on
    this dashboard produces one.
    """
    if not origin or origin == "null":
        return True
    if not host_header:
        return False
    return urlsplit(origin).netloc == host_header
