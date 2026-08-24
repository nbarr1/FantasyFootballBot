"""Read-only MFL API client.

This class deliberately has **no write methods of any kind**. Submitting to MFL
lives in :mod:`mflbot.mfl.write_client`, whose every method demands an approval
token. Analysis code is given an instance of *this* class, so "analysis
accidentally submits something" is not a bug that can be written -- the method
does not exist on the object it holds.

Caching, rate limiting and authentication are applied inside :meth:`export`, so
they cannot be bypassed by a caller taking a shortcut.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from ..config import LeagueRef
from ..errors import AuthError, ParseError, TransportError
from .auth import AuthState, Credentials, parse_login_response, redact
from .cache import ResponseCache, cache_key
from .endpoints import EndpointRegistry, Provenance
from .ratelimit import RateLimiter, RateLimitPolicy

log = logging.getLogger(__name__)

DEFAULT_USER_AGENT = "mflbot/0.1 (personal league assistant; contact via MFL account owner)"
#: Registering a client (MFL's API Client Registration page, plus SMS
#: validation) raises the request-rate ceiling roughly 2.5x over an
#: unregistered client -- but only when every request carries the exact
#: User-Agent string chosen at registration. Set this to use that string once
#: registered; the default below is unregistered and gets the lower, still
#: fully-functional, tier.
ENV_USER_AGENT = "MFLBOT_USER_AGENT"


def resolve_user_agent() -> str:
    return os.environ.get(ENV_USER_AGENT) or DEFAULT_USER_AGENT


def unwrap(payload: Any, *keys: str) -> Any:
    """Descend through MFL's nested JSON envelopes.

    MFL wraps collections twice -- ``{"rosters": {"franchise": [...]}}`` -- and
    collapses single-element lists to a bare object. This helper walks the given
    keys and always hands back a list where a collection was requested, so
    callers do not each reinvent the "is it a dict or a list this time" check.
    """
    node = payload
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            raise ParseError(
                f"Expected key '{key}' in MFL response; got "
                f"{type(node).__name__} with keys "
                f"{sorted(node)[:8] if isinstance(node, dict) else '<not a mapping>'}"
            )
        node = node[key]
    return node


def as_list(node: Any) -> list[Any]:
    """Normalise MFL's "one item is not a list" quirk."""
    if node is None:
        return []
    if isinstance(node, list):
        return node
    return [node]


@dataclass(slots=True)
class MFLResponse:
    """A parsed export response plus how it was obtained."""

    type_name: str
    payload: Any
    from_cache: bool
    age_seconds: float


class MFLReadClient:
    """Typed, cached, rate-limited wrapper around MFL's ``export`` API."""

    def __init__(
        self,
        league: LeagueRef,
        *,
        credentials: Credentials | None = None,
        registry: EndpointRegistry | None = None,
        cache: ResponseCache | None = None,
        rate_limiter: RateLimiter | None = None,
        transport: httpx.Client | None = None,
        cache_dir: Path | str = ".cache/mfl",
    ) -> None:
        self.league = league
        self.registry = registry or EndpointRegistry.load()
        self.cache = cache or ResponseCache(cache_dir)
        self.rate_limiter = rate_limiter or RateLimiter(RateLimitPolicy())
        self.auth = AuthState(credentials or Credentials.from_env())
        self._client = transport or httpx.Client(
            timeout=httpx.Timeout(30.0),
            headers={"User-Agent": resolve_user_agent()},
            follow_redirects=True,
        )
        self._owns_transport = transport is None

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        if self._owns_transport:
            self._client.close()

    def __enter__(self) -> MFLReadClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- authentication ----------------------------------------------------

    def is_authenticated(self) -> bool:
        return self.auth.is_authenticated

    def can_write(self) -> bool:
        return self.auth.can_write

    def login(self) -> bool:
        """Exchange username/password for a session cookie.

        Returns False (without raising) when no login credentials are
        configured, so that an API-key-only setup can still read. Raises
        :class:`AuthError` when credentials *are* present but rejected -- a
        wrong password must never degrade quietly into anonymous access.
        """
        creds = self.auth.credentials
        if not creds.has_login:
            return False
        # Login takes no league parameter, and MFL's own documented example
        # calls it against the api host (api.myfantasyleague.com/{year}/login),
        # not a league-specific one.
        url = f"{self.league.global_base_url}/login"
        params = {"USERNAME": creds.username, "PASSWORD": creds.password, "XML": "1"}
        self.rate_limiter.acquire()
        try:
            response = self._client.get(url, params=params)
        except httpx.HTTPError as exc:
            raise TransportError(f"Login request to MFL failed: {exc}") from exc
        if response.status_code != 200:
            raise AuthError(f"MFL login returned HTTP {response.status_code}")
        self.auth.session_cookie = parse_login_response(response.text)
        log.info("Authenticated to MFL as %s", creds.username)
        return True

    def ensure_authenticated(self, what: str) -> None:
        if self.auth.can_write:
            return
        if self.auth.credentials.has_login and not self.auth.session_cookie:
            self.login()
        self.auth.require_authenticated(what)

    # -- core request path -------------------------------------------------

    def export(
        self,
        type_name: str,
        *,
        force_refresh: bool = False,
        **params: Any,
    ) -> MFLResponse:
        """Issue (or serve from cache) one ``export?TYPE=...`` request."""
        endpoint = self.registry.read(type_name)

        unknown = {k for k in params if k.upper() not in {p.upper() for p in endpoint.params}}
        if unknown:
            raise ValueError(
                f"export TYPE={type_name} does not accept parameter(s) "
                f"{sorted(unknown)}; documented parameters are {list(endpoint.params)}"
            )

        if endpoint.requires_auth:
            self.ensure_authenticated(f"export TYPE={type_name}")

        request_params: dict[str, Any] = {
            k.upper(): v for k, v in params.items() if v is not None
        }
        request_params["TYPE"] = type_name
        request_params["JSON"] = "1"

        key = cache_key(type_name, request_params)
        if not force_refresh:
            cached = self.cache.get(key)
            if cached is not None:
                log.debug("cache hit %s (age %.0fs)", type_name, cached.age_seconds)
                return MFLResponse(type_name, cached.payload, True, cached.age_seconds)

        request_params.update(self.auth.request_params())
        # MFL requires (and recommends, for load-spreading) that requests with
        # no league parameter go to the api host rather than a league-specific
        # one; see LeagueRef.global_base_url.
        base_url = (
            self.league.base_url if endpoint.is_league_scoped else self.league.global_base_url
        )
        payload = self._request_with_retries(
            f"{base_url}/export", request_params, type_name
        )
        self.cache.put(key, payload, endpoint.ttl_seconds)
        return MFLResponse(type_name, payload, False, 0.0)

    def _request_with_retries(
        self, url: str, params: dict[str, Any], type_name: str
    ) -> Any:
        attempt = 0
        while True:
            self.rate_limiter.acquire()
            try:
                response = self._client.get(
                    url, params=params, headers=self.auth.request_headers()
                )
            except httpx.HTTPError as exc:
                if attempt >= self.rate_limiter.policy.max_retries - 1:
                    raise TransportError(
                        f"export TYPE={type_name} failed: {exc}"
                    ) from exc
                delay = self.rate_limiter.penalise(attempt)
                log.warning("transport error on %s, backing off %.1fs", type_name, delay)
                attempt += 1
                continue

            if response.status_code == 429 or response.status_code >= 500:
                retry_after = _retry_after_seconds(response)
                delay = self.rate_limiter.penalise(attempt, retry_after)
                log.warning(
                    "MFL returned %s for %s; backing off %.1fs",
                    response.status_code,
                    type_name,
                    delay,
                )
                attempt += 1
                continue

            if response.status_code != 200:
                raise TransportError(
                    f"export TYPE={type_name} returned HTTP {response.status_code}: "
                    f"{redact(response.text[:300])}"
                )

            self.rate_limiter.reset_penalty()
            return self._parse(response, type_name)

    def _parse(self, response: httpx.Response, type_name: str) -> Any:
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise ParseError(
                f"export TYPE={type_name} did not return JSON. "
                f"Body began: {redact(response.text[:200])}"
            ) from exc
        # MFL reports application-level failures with HTTP 200 and an error body.
        if isinstance(payload, dict) and "error" in payload:
            error = payload["error"]
            message = error.get("$t", error) if isinstance(error, dict) else error
            raise TransportError(f"MFL error on TYPE={type_name}: {message}")
        return payload

    # -- typed reads -------------------------------------------------------
    # Thin, named wrappers. They exist so that call sites read as domain
    # operations and so that parameter names stay in one place.

    def league_settings(self, **kw: Any) -> Any:
        return self.export("league", L=self.league.id, **kw).payload

    def scoring_rules(self, **kw: Any) -> Any:
        return self.export("rules", L=self.league.id, **kw).payload

    def all_rule_definitions(self, **kw: Any) -> Any:
        """Catalogue of scoring-event abbreviations. Not league-specific."""
        return self.export("allRules", **kw).payload

    def players(self, *, details: bool = True, since: int | None = None, **kw: Any) -> Any:
        return self.export(
            "players", DETAILS="1" if details else "0", SINCE=since, **kw
        ).payload

    def rosters(self, franchise: str | None = None, **kw: Any) -> Any:
        return self.export("rosters", L=self.league.id, FRANCHISE=franchise, **kw).payload

    def free_agents(self, position: str | None = None, **kw: Any) -> Any:
        return self.export("freeAgents", L=self.league.id, POSITION=position, **kw).payload

    def transactions(
        self,
        *,
        trans_type: str | None = None,
        franchise: str | None = None,
        days: int | None = None,
        count: int | None = None,
        **kw: Any,
    ) -> Any:
        return self.export(
            "transactions",
            L=self.league.id,
            TRANS_TYPE=trans_type,
            FRANCHISE=franchise,
            DAYS=days,
            COUNT=count,
            **kw,
        ).payload

    def player_scores(self, week: int | str, **kw: Any) -> Any:
        return self.export("playerScores", L=self.league.id, W=week, **kw).payload

    def projected_scores(self, week: int | str, **kw: Any) -> Any:
        return self.export("projectedScores", L=self.league.id, W=week, **kw).payload

    def live_scoring(self, week: int | str, **kw: Any) -> Any:
        return self.export("liveScoring", L=self.league.id, W=week, **kw).payload

    def weekly_results(self, week: int | str, **kw: Any) -> Any:
        return self.export("weeklyResults", L=self.league.id, W=week, **kw).payload

    def injuries(self, week: int | str | None = None, **kw: Any) -> Any:
        return self.export("injuries", W=week, **kw).payload

    def nfl_schedule(self, week: int | str | None = None, **kw: Any) -> Any:
        return self.export("nflSchedule", W=week, **kw).payload

    def trade_bait(self, **kw: Any) -> Any:
        return self.export("tradeBait", L=self.league.id, **kw).payload

    def assets(self, **kw: Any) -> Any:
        return self.export("assets", L=self.league.id, **kw).payload

    def league_standings(self, **kw: Any) -> Any:
        return self.export("leagueStandings", L=self.league.id, **kw).payload

    def adp(self, **kw: Any) -> Any:
        return self.export("adp", **kw).payload

    def pending_trades(self, franchise: str | None = None, **kw: Any) -> Any:
        """Trade offers awaiting a response.

        The TYPE name for this endpoint is not independently corroborated; it is
        marked UNVERIFIED in the registry and confirmed by
        ``bot verify-endpoints``. A wrong name here fails loudly on a read,
        which is why it is allowed to ship unverified while writes are not.
        """
        endpoint = self.registry.read("pendingTrades")
        if endpoint.provenance is Provenance.UNVERIFIED:
            log.debug(
                "pendingTrades endpoint name is unverified; run 'bot verify-endpoints'"
            )
        return self.export(
            "pendingTrades",
            L=self.league.id,
            FRANCHISE=franchise or self.league.franchise_id,
            **kw,
        ).payload


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None
