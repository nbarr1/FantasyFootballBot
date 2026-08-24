"""MFL authentication.

Two mechanisms are supported, selected by which environment variables are set:

``MFLBOT_MFL_API_KEY``
    A per-league API key generated from MFL's Developer API page. Sent as the
    ``APIKEY`` request parameter. Grants access to private-league data.

``MFLBOT_MFL_USERNAME`` + ``MFLBOT_MFL_PASSWORD``
    Exchanged once at ``/{season}/login`` for a session cookie value, which is
    then sent as ``Cookie: MFL_USER_ID=<value>`` on subsequent requests.

If both are present the API key is used for reads and the login cookie is
obtained as well, because franchise-scoped and write operations are tied to a
logged-in user.

Secrets are read only from the environment (or a mode-0600 secrets file), are
never written to the database, never included in an audit-log entry, and never
rendered by the approval interface. :func:`redact` exists so that URLs can be
logged safely.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from ..errors import AuthError, ConfigError

ENV_API_KEY = "MFLBOT_MFL_API_KEY"
ENV_USERNAME = "MFLBOT_MFL_USERNAME"
ENV_PASSWORD = "MFLBOT_MFL_PASSWORD"
ENV_SECRETS_FILE = "MFLBOT_SECRETS_FILE"

#: Cookie name MFL sets for an authenticated user session.
SESSION_COOKIE_NAME = "MFL_USER_ID"

_SECRET_PARAM_RE = re.compile(
    r"(?i)\b(APIKEY|PASSWORD|USERNAME)=([^&\s]+)"
)


def redact(text: str) -> str:
    """Blank out credential-bearing query parameters in ``text``.

    Every log line and audit record that could contain a request URL passes
    through this first.
    """
    return _SECRET_PARAM_RE.sub(lambda m: f"{m.group(1)}=<redacted>", text)


def _load_secrets_file(path: Path) -> dict[str, str]:
    """Read ``KEY=value`` lines from a restricted-permission secrets file."""
    if not path.exists():
        raise ConfigError(f"Secrets file {path} does not exist")
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ConfigError(
            f"Secrets file {path} is group/world accessible. "
            f"Run: chmod 600 {path}"
        )
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


@dataclass(frozen=True, slots=True)
class Credentials:
    """Whatever credentials the environment actually provides. Possibly none."""

    api_key: str | None = None
    username: str | None = None
    password: str | None = None

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Credentials:
        source = dict(os.environ if env is None else env)
        secrets_path = source.get(ENV_SECRETS_FILE)
        if secrets_path:
            # Environment wins over file, so an operator can override ad hoc.
            file_values = _load_secrets_file(Path(secrets_path))
            for key, value in file_values.items():
                source.setdefault(key, value)
        return cls(
            api_key=source.get(ENV_API_KEY) or None,
            username=source.get(ENV_USERNAME) or None,
            password=source.get(ENV_PASSWORD) or None,
        )

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key)

    @property
    def has_login(self) -> bool:
        return bool(self.username and self.password)

    @property
    def is_empty(self) -> bool:
        return not (self.has_api_key or self.has_login)

    def describe(self) -> str:
        """Human-readable summary that reveals no secret material."""
        parts = []
        if self.has_api_key:
            parts.append(f"API key ({ENV_API_KEY}, {len(self.api_key or '')} chars)")
        if self.has_login:
            parts.append(f"login for user '{self.username}'")
        return " + ".join(parts) if parts else "none"

    def __repr__(self) -> str:  # pragma: no cover - defensive
        return f"Credentials({self.describe()})"


@dataclass(slots=True)
class AuthState:
    """Resolved authentication material for an active client session."""

    credentials: Credentials
    session_cookie: str | None = None

    @property
    def is_authenticated(self) -> bool:
        """True when the client can act as the user, not just read public data.

        Deliberately strict: an API key alone unlocks private-league *reads*,
        but franchise-scoped state and any write need the user session.
        """
        return bool(self.session_cookie) or self.credentials.has_api_key

    @property
    def can_write(self) -> bool:
        """Writes require a real user session, never an API key alone."""
        return bool(self.session_cookie)

    def request_params(self) -> dict[str, str]:
        return {"APIKEY": self.credentials.api_key} if self.credentials.api_key else {}

    def request_headers(self) -> dict[str, str]:
        if not self.session_cookie:
            return {}
        return {"Cookie": f"{SESSION_COOKIE_NAME}={self.session_cookie}"}

    def require_authenticated(self, what: str) -> None:
        if not self.is_authenticated:
            raise AuthError(
                f"{what} requires authentication, and no credentials are configured.\n"
                f"  Set {ENV_API_KEY}, or {ENV_USERNAME} and {ENV_PASSWORD}.\n"
                f"  The bot refuses to fall back to unauthenticated data here, because "
                f"rosters, lineups and pending trades differ silently when anonymous."
            )

    def require_writable(self, what: str) -> None:
        if not self.can_write:
            raise AuthError(
                f"{what} requires a logged-in MFL user session.\n"
                f"  Set {ENV_USERNAME} and {ENV_PASSWORD}. An API key alone is not "
                f"sufficient to act on a franchise's behalf."
            )


def parse_login_response(body: str) -> str:
    """Extract the session cookie value from a ``/login`` response.

    MFL replies with a status document containing ``cookie_name="cookie_value"``.
    Both the XML and JSON forms are handled, since the response format has
    varied. Raises :class:`AuthError` on a rejected login rather than returning
    an empty cookie that would fail confusingly much later.
    """
    text = body.strip()
    if not text:
        raise AuthError("MFL login returned an empty response")

    lowered = text.lower()
    if "error" in lowered and SESSION_COOKIE_NAME.lower() not in lowered:
        raise AuthError(f"MFL rejected the login: {redact(text[:400])}")

    # Preferred shape: an explicit MFL_USER_ID assignment. The optional quote
    # after the key name covers the JSON form ("MFL_USER_ID": "...") as well as
    # the XML attribute form (MFL_USER_ID="...").
    match = re.search(
        rf'{SESSION_COOKIE_NAME}["\']?\s*[=:]\s*["\']?([^"\'&<>\s,}}]+)', text
    )
    if match:
        return match.group(1)

    # Fallback shape: a cookie_name / cookie_value pair.
    name = re.search(r'cookie_name["\']?\s*[=:]\s*["\']([^"\']+)["\']', text)
    value = re.search(r'cookie_value["\']?\s*[=:]\s*["\']([^"\']+)["\']', text)
    if name and value and name.group(1) == SESSION_COOKIE_NAME:
        return value.group(1)

    raise AuthError(
        "Could not find a session cookie in MFL's login response. "
        f"Response began: {redact(text[:200])}"
    )
