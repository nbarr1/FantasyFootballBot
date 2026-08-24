"""Client-level policy: caching, rate limiting, credential hygiene.

These are enforced inside the client so that no caller can opt out of them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mflbot.config import LeagueRef
from mflbot.errors import AuthError, ParseError, TransportError
from mflbot.mfl.auth import Credentials, parse_login_response, redact
from mflbot.mfl.cache import ResponseCache, cache_key
from mflbot.mfl.client import MFLReadClient, as_list, unwrap
from mflbot.mfl.ratelimit import RateLimiter, RateLimitPolicy


class FakeResponse:
    def __init__(self, payload, status_code=200, headers=None, text=None) -> None:
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text if text is not None else json.dumps(payload)

    def json(self):
        if self._payload is None:
            raise json.JSONDecodeError("no json", "", 0)
        return self._payload


class RecordingTransport:
    """Counts requests so cache behaviour is observable."""

    def __init__(self, responses) -> None:
        self._responses = list(responses)
        self.requests: list[tuple[str, dict]] = []

    def get(self, url, params=None, headers=None):
        self.requests.append((url, dict(params or {})))
        return self._responses.pop(0) if self._responses else FakeResponse({})

    def close(self):
        return None


@pytest.fixture
def league() -> LeagueRef:
    return LeagueRef("TEST0001", 2026, "example.invalid")


def make_client(league, transport, tmp_path: Path, **kw) -> MFLReadClient:
    return MFLReadClient(
        league,
        credentials=kw.pop("credentials", Credentials()),
        transport=transport,
        cache=ResponseCache(tmp_path / "cache"),
        rate_limiter=RateLimiter(
            RateLimitPolicy(min_interval_seconds=0, max_requests=1000)
        ),
        **kw,
    )


# -- credential hygiene -----------------------------------------------------

def test_redact_removes_every_credential_bearing_parameter() -> None:
    url = "https://x/2026/export?TYPE=league&APIKEY=sekret&PASSWORD=hunter2&USERNAME=bob"
    cleaned = redact(url)
    for secret in ("sekret", "hunter2", "bob"):
        assert secret not in cleaned
    assert "TYPE=league" in cleaned


def test_credentials_repr_never_leaks_the_secret() -> None:
    creds = Credentials(api_key="super-secret-key", username="bob", password="hunter2")
    rendered = repr(creds) + creds.describe()
    assert "super-secret-key" not in rendered
    assert "hunter2" not in rendered


def test_cache_key_excludes_credentials_so_no_secret_reaches_a_filename() -> None:
    with_key = cache_key("league", {"L": "1", "APIKEY": "sekret"})
    without = cache_key("league", {"L": "1"})
    assert with_key == without
    assert "sekret" not in with_key


def test_rejected_login_raises_rather_than_degrading_to_anonymous() -> None:
    with pytest.raises(AuthError):
        parse_login_response("<error>Invalid password</error>")


def test_login_response_parses_both_documented_shapes() -> None:
    assert parse_login_response('<status MFL_USER_ID="abc123"></status>') == "abc123"
    assert parse_login_response('{"cookie_name":"MFL_USER_ID","cookie_value":"xyz"}') == "xyz"


# -- caching ----------------------------------------------------------------

def test_repeated_reads_hit_the_cache_not_the_network(league, tmp_path) -> None:
    transport = RecordingTransport([FakeResponse({"league": {"id": "TEST0001"}})])
    client = make_client(league, transport, tmp_path)

    first = client.export("league", L="TEST0001")
    second = client.export("league", L="TEST0001")

    assert len(transport.requests) == 1, "the second read should have been cached"
    assert first.from_cache is False and second.from_cache is True
    assert second.payload == first.payload


def test_force_refresh_bypasses_the_cache(league, tmp_path) -> None:
    transport = RecordingTransport(
        [FakeResponse({"league": {"id": "a"}}), FakeResponse({"league": {"id": "b"}})]
    )
    client = make_client(league, transport, tmp_path)
    client.export("league", L="TEST0001")
    refreshed = client.export("league", L="TEST0001", force_refresh=True)

    assert len(transport.requests) == 2
    assert refreshed.payload["league"]["id"] == "b"


def test_player_database_ttl_is_at_least_a_day(league, tmp_path) -> None:
    """MFL asks for the player file at most once daily; the TTL enforces it."""
    client = make_client(league, RecordingTransport([]), tmp_path)
    assert client.registry.read("players").ttl_seconds >= 86_400


def test_expired_entries_are_a_miss(tmp_path) -> None:
    cache = ResponseCache(tmp_path / "c")
    cache.put("k", {"v": 1}, ttl_seconds=-1)
    assert cache.get("k") is None
    assert cache.get("k", allow_stale=True) is not None


def test_corrupt_cache_file_is_a_miss_not_a_crash(tmp_path) -> None:
    cache = ResponseCache(tmp_path / "c")
    cache.put("k", {"v": 1}, ttl_seconds=600)
    (tmp_path / "c" / "k.json").write_text("{not json", encoding="utf-8")
    assert cache.get("k") is None


# -- request validation and errors -----------------------------------------

def test_undocumented_parameters_are_rejected_before_a_request_is_sent(
    league, tmp_path
) -> None:
    transport = RecordingTransport([])
    client = make_client(league, transport, tmp_path)
    with pytest.raises(ValueError, match="does not accept parameter"):
        client.export("league", L="TEST0001", NONSENSE="1")
    assert transport.requests == []


def test_unknown_export_type_is_rejected(league, tmp_path) -> None:
    client = make_client(league, RecordingTransport([]), tmp_path)
    with pytest.raises(KeyError, match="Unknown MFL export type"):
        client.export("notARealEndpoint")


def test_application_level_error_body_raises_despite_http_200(league, tmp_path) -> None:
    transport = RecordingTransport([FakeResponse({"error": {"$t": "Invalid league"}})])
    client = make_client(league, transport, tmp_path)
    with pytest.raises(TransportError, match="Invalid league"):
        client.export("league", L="TEST0001")


def test_non_json_response_raises_a_clear_parse_error(league, tmp_path) -> None:
    transport = RecordingTransport([FakeResponse(None, text="<html>nope</html>")])
    client = make_client(league, transport, tmp_path)
    with pytest.raises(ParseError, match="did not return JSON"):
        client.export("league", L="TEST0001")


def test_rate_limited_response_backs_off_then_succeeds(league, tmp_path) -> None:
    transport = RecordingTransport(
        [
            FakeResponse({}, status_code=429, headers={"Retry-After": "0"}),
            FakeResponse({"league": {"id": "TEST0001"}}),
        ]
    )
    client = make_client(league, transport, tmp_path)
    result = client.export("league", L="TEST0001")
    assert len(transport.requests) == 2
    assert result.payload["league"]["id"] == "TEST0001"


def test_authenticated_endpoint_refuses_without_credentials(league, tmp_path) -> None:
    transport = RecordingTransport([])
    client = make_client(league, transport, tmp_path)
    with pytest.raises(AuthError, match="requires authentication"):
        client.export("rosters", L="TEST0001")
    assert transport.requests == [], "no anonymous fallback request may be sent"


def test_api_key_is_sent_as_a_parameter_when_present(league, tmp_path) -> None:
    transport = RecordingTransport([FakeResponse({"rosters": {}})])
    client = make_client(
        league, transport, tmp_path, credentials=Credentials(api_key="k")
    )
    client.export("rosters", L="TEST0001")
    assert transport.requests[0][1]["APIKEY"] == "k"


# -- response shape helpers -------------------------------------------------

def test_as_list_normalises_mfls_single_item_collapse() -> None:
    assert as_list([1, 2]) == [1, 2]
    assert as_list({"a": 1}) == [{"a": 1}]
    assert as_list(None) == []


def test_unwrap_descends_and_reports_a_missing_key_clearly() -> None:
    assert unwrap({"a": {"b": [1]}}, "a", "b") == [1]
    with pytest.raises(ParseError, match="Expected key 'c'"):
        unwrap({"a": {"b": [1]}}, "a", "c")


# -- rate limiter -----------------------------------------------------------

def test_limiter_enforces_a_minimum_interval() -> None:
    now = [0.0]
    slept: list[float] = []

    def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    limiter = RateLimiter(
        RateLimitPolicy(min_interval_seconds=2.0, max_requests=100),
        sleep=sleep,
        monotonic=lambda: now[0],
    )
    limiter.acquire()
    limiter.acquire()
    assert slept and sum(slept) >= 2.0


def test_limiter_gives_up_after_the_retry_budget() -> None:
    from mflbot.errors import RateLimitError

    limiter = RateLimiter(RateLimitPolicy(max_retries=2))
    limiter.penalise(0)
    limiter.penalise(1)
    with pytest.raises(RateLimitError, match="Reduce polling frequency"):
        limiter.penalise(2)


# -- host routing -------------------------------------------------------

def test_league_scoped_reads_go_to_the_configured_league_host(league, tmp_path) -> None:
    transport = RecordingTransport([FakeResponse({"league": {"id": "TEST0001"}})])
    client = make_client(league, transport, tmp_path)
    client.export("league", L="TEST0001")
    assert transport.requests[0][0].startswith(f"https://{league.host}/")


def test_reads_with_no_league_parameter_go_to_the_api_host(league, tmp_path) -> None:
    """MFL: "if the request does not take a league parameter (L=), it must be
    sent to the host api" -- also documented as best practice, to spread load."""
    transport = RecordingTransport([FakeResponse({"players": {}})])
    client = make_client(league, transport, tmp_path)
    client.export("players")
    url = transport.requests[0][0]
    assert url.startswith("https://api.myfantasyleague.com/"), url
    assert league.host not in url


def test_login_goes_to_the_api_host_not_the_league_host(league, tmp_path) -> None:
    """Matches MFL's own documented login example, which calls
    api.myfantasyleague.com/{year}/login rather than a league-specific host."""
    transport = RecordingTransport(
        [FakeResponse({}, text='<status MFL_USER_ID="abc123"></status>')]
    )
    client = make_client(
        league, transport, tmp_path,
        credentials=Credentials(username="u", password="p"),
    )
    client.login()
    assert transport.requests[0][0].startswith("https://api.myfantasyleague.com/")


# -- API key restricted to reads -----------------------------------------

def test_api_key_is_never_sent_on_a_write(league, tmp_path) -> None:
    """MFL: the APIKEY alternate-auth path "does not work for import requests,
    only export". Sending it on a write is at best misleading, at worst wrong
    if MFL's handling of it there ever changes; it must never be sent."""
    from mflbot.approval.token import TokenService
    from mflbot.mfl.auth import AuthState
    from mflbot.mfl.endpoints import Capability, EndpointRegistry, Provenance, WriteEndpoint
    from mflbot.mfl.write_client import MFLWriteClient
    from mflbot.recommend.models import LineupPayload
    from mflbot.storage.db import Database

    db = Database(":memory:")
    db.migrate()
    tokens = TokenService(db=db, secret=b"test-only-signing-key")

    registry = EndpointRegistry()
    registry.writes[Capability.SUBMIT_LINEUP] = WriteEndpoint(
        capability=Capability.SUBMIT_LINEUP,
        candidates=("lineup",),
        description="synthetic",
        type_name="lineup",
        params=("L", "W", "FRANCHISE", "STARTERS"),
        provenance=Provenance.DOC_VERIFIED,
        field_map={
            "league_id": "L", "week": "W",
            "franchise_id": "FRANCHISE", "starter_ids": "STARTERS",
        },
    )

    posted: dict = {}

    class RecordingPostTransport:
        def post(self, url, data=None, headers=None):
            posted["data"] = data
            posted["headers"] = headers
            return FakeResponse({}, text="OK")

        def close(self):
            return None

    auth = AuthState(
        Credentials(api_key="leaked-api-key", username="u", password="p"),
        session_cookie="a-real-session-cookie",
    )
    client = MFLWriteClient(
        league, auth, tokens, registry=registry, transport=RecordingPostTransport()
    )
    payload = LineupPayload(
        capability=Capability.SUBMIT_LINEUP,
        league_id="TEST0001", franchise_id="0001", week=1, starter_ids=("p1",),
    )
    from mflbot.recommend.models import Confidence, Evidence, Recommendation, RecommendationKind
    from mflbot.recommend.store import RecommendationStore

    recommendation = Recommendation(
        kind=RecommendationKind.LINEUP, payload=payload, rationale="x",
        evidence=Evidence(), confidence=Confidence.HIGH,
    )
    RecommendationStore(db).save(recommendation)
    token = tokens.issue(recommendation, "test")

    client.submit(payload, token)

    assert "APIKEY" not in posted["data"], posted["data"]
    assert "leaked-api-key" not in str(posted["data"])
    # The cookie is still the (correct, documented) auth path for writes.
    assert "a-real-session-cookie" in posted["headers"]["Cookie"]
