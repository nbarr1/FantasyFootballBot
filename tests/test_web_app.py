"""The dashboard: access control, the approval path, and the job bridge.

These are the web-surface counterparts of ``test_approval.py``. The property
under test throughout is that adding a browser did not add a way around the
invariants: a request cannot approve without a session, cannot approve without
a CSRF token, cannot approve on behalf of another origin, cannot submit
something it edited after approving, and cannot reach any command that writes
to MFL through the action buttons.

The context is the real :class:`~mflbot.context.BotContext` driven against the
synthetic MFL fake from ``test_end_to_end`` -- so the pages render from data
that went through the production ingestion path, not from hand-built rows.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest
from test_end_to_end import (  # the synthetic MFL fake, reused rather than rebuilt
    LEAGUE_ID,
    SEASON,
    WEEK,
    FakeMFLTransport,
    ingest_everything,
)

from mflbot.approval.cli_channel import CLIApprovalChannel
from mflbot.approval.token import TokenService
from mflbot.config import (
    Config,
    LeagueRef,
    LineupSettings,
    NotifySettings,
    StorageSettings,
    TradeSettings,
    WaiverSettings,
    WebSettings,
)
from mflbot.context import BotContext
from mflbot.mfl.cache import ResponseCache
from mflbot.mfl.client import MFLReadClient
from mflbot.mfl.endpoints import EndpointRegistry
from mflbot.mfl.ratelimit import RateLimiter, RateLimitPolicy
from mflbot.notify.base import NullNotifier
from mflbot.recommend.models import RecommendationStatus
from mflbot.recommend.store import RecommendationStore
from mflbot.storage.db import Database
from mflbot.storage.repositories import Repositories
from mflbot.web import build_app
from mflbot.web.jobs import (
    FORBIDDEN_COMMANDS,
    JOB_SPECS,
    RUNNABLE_COMMANDS,
    JobManager,
)
from mflbot.web.security import SESSION_COOKIE, WebSecurity

fastapi_testclient = pytest.importorskip("fastapi.testclient")
TestClient = fastapi_testclient.TestClient

PASSWORD = "test-only-dashboard-password"


@pytest.fixture
def context(tmp_path) -> BotContext:
    config = Config(
        league=LeagueRef(LEAGUE_ID, SEASON, "example.invalid", franchise_id="0001"),
        storage=StorageSettings(path=str(tmp_path / "web.db")),
        waivers=WaiverSettings(),
        trades=TradeSettings(),
        lineup=LineupSettings(),
        notify=NotifySettings(enabled=False),
        web=WebSettings(),
    )
    db = Database(":memory:")
    db.migrate()
    client = MFLReadClient(
        config.league,
        registry=EndpointRegistry.load("does-not-exist.json"),
        cache=ResponseCache(tmp_path / "cache"),
        rate_limiter=RateLimiter(RateLimitPolicy(min_interval_seconds=0)),
        transport=FakeMFLTransport(),
    )
    client.auth.session_cookie = "synthetic-session"
    store = RecommendationStore(db)
    tokens = TokenService(db=db, secret=b"web-test-key")
    ctx = BotContext(
        config=config,
        db=db,
        repos=Repositories(db),
        store=store,
        tokens=tokens,
        client=client,
        registry=client.registry,
        notifier=NullNotifier("disabled in tests"),
        channel=CLIApprovalChannel(store, tokens, writer=lambda _: None),
    )
    ingest_everything(ctx)
    ctx.run_lineup_analysis(week=WEEK)
    yield ctx
    db.close()


def make_client(context, **kwargs) -> TestClient:
    app = build_app(context, WebSecurity.create(PASSWORD), **kwargs)
    return TestClient(app)


@pytest.fixture
def client(context) -> TestClient:
    with make_client(context) as test_client:
        yield test_client


@pytest.fixture
def signed_in(client) -> TestClient:
    response = client.post("/login", data={"secret": PASSWORD}, follow_redirects=False)
    assert response.status_code == 303, response.text
    assert client.cookies.get(SESSION_COOKIE)
    return client


def csrf_token(signed_in: TestClient) -> str:
    """Pull the token out of a rendered form, as a browser would."""
    body = signed_in.get("/").text
    marker = 'name="csrf_token" value="'
    start = body.index(marker) + len(marker)
    return body[start : body.index('"', start)]


def only_recommendation(context):
    pending = context.store.pending()
    assert len(pending) == 1, pending
    return pending[0]


# ---------------------------------------------------------------------------
# access control
# ---------------------------------------------------------------------------

def test_no_dashboard_without_authentication(client, context) -> None:
    page = client.get("/", follow_redirects=False)
    assert page.status_code == 303
    assert page.headers["location"].startswith("/login")

    api = client.get("/api/state")
    assert api.status_code == 401

    # Not even the id of a recommendation leaks to an anonymous request.
    detail = client.get(
        f"/recommendations/{only_recommendation(context).id}", follow_redirects=False
    )
    assert detail.status_code == 303


def test_there_is_no_unauthenticated_mode() -> None:
    with pytest.raises(ValueError, match="no unauthenticated mode"):
        WebSecurity(password_hash=None, access_token=None)


def test_the_wrong_password_is_refused(client) -> None:
    response = client.post("/login", data={"secret": "not-it"})
    assert response.status_code == 401
    assert "not the right password" in response.text
    assert not client.cookies.get(SESSION_COOKIE)


def test_repeated_failures_are_throttled(client) -> None:
    for _ in range(10):
        client.post("/login", data={"secret": "wrong"})
    refused = client.post("/login", data={"secret": PASSWORD})
    assert refused.status_code == 401
    assert "Too many failed attempts" in refused.text


def test_the_access_token_link_signs_you_in(context) -> None:
    security = WebSecurity.create(None)
    with TestClient(build_app(context, security)) as client:
        response = client.get(
            f"/login?token={security.access_token}", follow_redirects=False
        )
        assert response.status_code == 303
        assert client.cookies.get(SESSION_COOKIE)
        assert client.get("/").status_code == 200


@pytest.mark.parametrize(
    "target", ["https://evil.example/", "//evil.example/", "http://evil.example"]
)
def test_login_cannot_be_turned_into_an_open_redirect(client, target: str) -> None:
    response = client.post(
        "/login", data={"secret": PASSWORD, "next": target}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_a_post_redirect_stays_on_this_site(signed_in) -> None:
    response = signed_in.post(
        "/actions/status/run?next=https://evil.example",
        data={"csrf_token": csrf_token(signed_in)},
        follow_redirects=False,
    )
    assert response.headers["location"].startswith("/")
    signed_in.app.state.jobs.stop()


def test_signing_out_ends_the_session(signed_in) -> None:
    signed_in.post(
        "/logout", data={"csrf_token": csrf_token(signed_in)}, follow_redirects=False
    )
    assert signed_in.get("/", follow_redirects=False).status_code == 303


def test_healthz_is_public_and_says_nothing_about_the_league(client) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_every_page_renders(signed_in) -> None:
    for path in ("/", "/recommendations", "/team", "/league", "/actions", "/audit"):
        response = signed_in.get(path)
        assert response.status_code == 200, (path, response.status_code)
        assert "<html" in response.text


def test_a_missing_recommendation_is_a_404_page(signed_in) -> None:
    response = signed_in.get("/recommendations/nope")
    assert response.status_code == 404
    assert "No such recommendation" in response.text


# ---------------------------------------------------------------------------
# CSRF and cross-site
# ---------------------------------------------------------------------------

def test_approval_without_a_csrf_token_is_refused(signed_in, context) -> None:
    recommendation = only_recommendation(context)
    response = signed_in.post(f"/recommendations/{recommendation.id}/approve", data={})
    assert response.status_code == 403
    assert context.store.get(recommendation.id).status == RecommendationStatus.PROPOSED
    assert context.tokens.latest_for(recommendation.id) is None


def test_a_stale_csrf_token_is_refused(signed_in, context) -> None:
    recommendation = only_recommendation(context)
    response = signed_in.post(
        f"/recommendations/{recommendation.id}/approve",
        data={"csrf_token": "a-token-from-somewhere-else"},
    )
    assert response.status_code == 403
    assert context.store.get(recommendation.id).status == RecommendationStatus.PROPOSED


def test_a_cross_site_post_is_refused(signed_in, context) -> None:
    recommendation = only_recommendation(context)
    response = signed_in.post(
        f"/recommendations/{recommendation.id}/approve",
        data={"csrf_token": csrf_token(signed_in)},
        headers={"Origin": "http://evil.example"},
    )
    assert response.status_code == 403
    assert context.store.get(recommendation.id).status == RecommendationStatus.PROPOSED


# ---------------------------------------------------------------------------
# the approval path
# ---------------------------------------------------------------------------

def test_approving_mints_a_token_and_submits_nothing(signed_in, context) -> None:
    recommendation = only_recommendation(context)
    response = signed_in.post(
        f"/recommendations/{recommendation.id}/approve",
        data={"csrf_token": csrf_token(signed_in)},
        follow_redirects=False,
    )
    assert response.status_code == 303

    stored = context.store.get(recommendation.id)
    assert stored.status == RecommendationStatus.APPROVED
    token = context.tokens.latest_for(recommendation.id)
    assert token is not None
    assert token.payload_hash == stored.payload_hash
    assert token.approved_by.startswith("web:")
    # Approval is not submission: nothing has been sent, and the audit trail
    # -- which is written before any attempt -- is still empty.
    assert context.repos.audit_entries() == []


def test_approving_one_leaves_the_others_alone(signed_in, context) -> None:
    context.run_waiver_analysis(week=WEEK)
    pending = context.store.pending()
    assert len(pending) > 1, "this test needs more than one pending recommendation"
    chosen = pending[0]

    signed_in.post(
        f"/recommendations/{chosen.id}/approve",
        data={"csrf_token": csrf_token(signed_in)},
    )
    for other in pending[1:]:
        assert context.store.get(other.id).status == RecommendationStatus.PROPOSED
        assert context.tokens.latest_for(other.id) is None


def test_editing_after_approving_invalidates_the_approval(signed_in, context) -> None:
    recommendation = only_recommendation(context)
    token_form = csrf_token(signed_in)
    signed_in.post(
        f"/recommendations/{recommendation.id}/approve", data={"csrf_token": token_form}
    )
    issued = context.tokens.latest_for(recommendation.id)
    assert issued is not None

    signed_in.post(
        f"/recommendations/{recommendation.id}/edit",
        data={"csrf_token": token_form, "field_week": str(WEEK + 1)},
    )
    edited = context.store.get(recommendation.id)
    assert edited.payload.week == WEEK + 1
    assert edited.payload_hash != issued.payload_hash

    # The token the earlier approval minted no longer describes this action.
    with pytest.raises(Exception, match="different action"):
        context.tokens.verify(issued, edited.payload_hash)


def test_an_edit_form_cannot_reach_an_identity_field(signed_in, context) -> None:
    recommendation = only_recommendation(context)
    original = recommendation.payload.franchise_id
    signed_in.post(
        f"/recommendations/{recommendation.id}/edit",
        data={
            "csrf_token": csrf_token(signed_in),
            "field_franchise_id": "9999",
            "field_league_id": "SOMEONE-ELSES-LEAGUE",
        },
    )
    stored = context.store.get(recommendation.id)
    assert stored.payload.franchise_id == original
    assert stored.payload.league_id == LEAGUE_ID


def test_rejecting_records_the_decision_and_submits_nothing(signed_in, context) -> None:
    recommendation = only_recommendation(context)
    signed_in.post(
        f"/recommendations/{recommendation.id}/reject",
        data={"csrf_token": csrf_token(signed_in), "note": "not this week"},
    )
    assert context.store.get(recommendation.id).status == RecommendationStatus.REJECTED
    assert context.tokens.latest_for(recommendation.id) is None
    assert context.repos.audit_entries() == []


def test_an_expired_recommendation_cannot_be_approved(signed_in, context) -> None:
    recommendation = only_recommendation(context)
    recommendation.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    context.store.save(recommendation)

    signed_in.post(
        f"/recommendations/{recommendation.id}/approve",
        data={"csrf_token": csrf_token(signed_in)},
    )
    assert context.store.get(recommendation.id).status != RecommendationStatus.APPROVED
    assert context.tokens.latest_for(recommendation.id) is None


def test_submitting_without_an_approval_is_refused(signed_in, context) -> None:
    recommendation = only_recommendation(context)
    response = signed_in.post(
        f"/recommendations/{recommendation.id}/submit",
        data={"csrf_token": csrf_token(signed_in)},
        follow_redirects=True,
    )
    assert "no live approval" in response.text
    assert context.repos.audit_entries() == []


def test_approve_and_submit_executes_exactly_once(signed_in, context, monkeypatch) -> None:
    calls = []

    class Outcome:
        ok = True
        message = "Confirmed."

    def fake_execute(self, recommendation, token):
        calls.append((recommendation.id, token.token_id))
        return Outcome()

    monkeypatch.setattr(BotContext, "execute_approved", fake_execute)
    recommendation = only_recommendation(context)
    signed_in.post(
        f"/recommendations/{recommendation.id}/approve",
        data={"csrf_token": csrf_token(signed_in), "submit_now": "1"},
    )
    assert len(calls) == 1
    assert calls[0][0] == recommendation.id


def test_no_submit_mode_records_approval_but_never_writes(context, monkeypatch) -> None:
    monkeypatch.setattr(
        BotContext,
        "execute_approved",
        lambda *a, **k: pytest.fail("--no-submit must not reach the executor"),
    )
    with make_client(context, allow_submissions=False) as client:
        client.post("/login", data={"secret": PASSWORD})
        token_form = csrf_token(client)
        recommendation = only_recommendation(context)
        client.post(
            f"/recommendations/{recommendation.id}/approve",
            data={"csrf_token": token_form, "submit_now": "1"},
        )
        assert (
            context.store.get(recommendation.id).status
            == RecommendationStatus.APPROVED
        )
        client.post(
            f"/recommendations/{recommendation.id}/submit", data={"csrf_token": token_form}
        )


# ---------------------------------------------------------------------------
# the job bridge
# ---------------------------------------------------------------------------

def test_no_runnable_command_can_write_to_mfl() -> None:
    assert not (RUNNABLE_COMMANDS & FORBIDDEN_COMMANDS)
    for command in ("approve", "execute", "reject", "edit", "serve", "run", "init"):
        assert command not in RUNNABLE_COMMANDS, command
    for spec in JOB_SPECS:
        assert spec.argv[0] in RUNNABLE_COMMANDS, spec.id


def test_every_job_spec_is_a_real_cli_invocation() -> None:
    from mflbot.cli import build_parser

    parser = build_parser()
    for spec in JOB_SPECS:
        args = parser.parse_args(spec.build_argv({}))
        assert callable(args.func), spec.id


def test_running_a_job_streams_the_command_output(context) -> None:
    from mflbot.web.events import EventBus

    bus = EventBus()
    manager = JobManager(context, bus)
    with bus.subscribe() as subscription:
        run = manager.submit("status", {}, actor="test")
        deadline = time.monotonic() + 20
        while not run.is_finished and time.monotonic() < deadline:
            time.sleep(0.05)
        manager.stop()

        assert run.status == "succeeded", run.lines
        assert any("Stored data" in line for line in run.lines)
        names = set()
        while (event := subscription.get(timeout=0)) is not None:
            names.add(event.name)
        assert {"job", "job.log"} <= names


def test_a_failing_job_is_reported_rather_than_swallowed(context) -> None:
    from mflbot.web.events import EventBus

    manager = JobManager(context, EventBus())
    # No scores are stored for this week, so validate-scoring exits non-zero.
    run = manager.submit("validate-scoring", {"week": "17"}, actor="test")
    deadline = time.monotonic() + 20
    while not run.is_finished and time.monotonic() < deadline:
        time.sleep(0.05)
    manager.stop()
    assert run.status == "failed"
    assert run.exit_code == 2


def test_a_bad_parameter_is_refused_before_anything_runs(context) -> None:
    from mflbot.web.events import EventBus

    manager = JobManager(context, EventBus())
    with pytest.raises(ValueError, match="whole number"):
        manager.submit("sync-projections", {"week": "next tuesday"}, actor="test")
    assert manager.runs() == []


def test_the_action_route_queues_the_job(signed_in, context) -> None:
    response = signed_in.post(
        "/actions/status/run",
        data={"csrf_token": csrf_token(signed_in)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    runs = signed_in.app.state.jobs.runs()
    assert runs and runs[0].spec_id == "status"
    signed_in.app.state.jobs.stop()


def test_an_unknown_action_is_a_404(signed_in) -> None:
    response = signed_in.post(
        "/actions/rm-rf/run", data={"csrf_token": csrf_token(signed_in)}
    )
    assert response.status_code == 404


def test_no_jobs_mode_refuses_to_run_anything(context) -> None:
    with make_client(context, allow_jobs=False) as client:
        client.post("/login", data={"secret": PASSWORD})
        response = client.post(
            "/actions/status/run",
            data={"csrf_token": csrf_token(client)},
            follow_redirects=True,
        )
        assert "--no-jobs" in response.text
        assert client.app.state.jobs.runs() == []


# ---------------------------------------------------------------------------
# what the pages show
# ---------------------------------------------------------------------------

def test_the_dashboard_shows_the_pending_action_and_its_reasoning(
    signed_in, context
) -> None:
    recommendation = only_recommendation(context)
    body = signed_in.get(f"/recommendations/{recommendation.id}").text
    # The literal payload, not only a summary.
    assert "starter_ids" in body
    assert recommendation.payload_hash[:16] in body
    # Player ids are resolved to names, without the ids disappearing.
    assert "p-qb1" in body
    assert "Synthetic QB One" in body
    assert "Projections are estimates" in body


def test_the_team_page_never_shows_a_missing_projection_as_zero(
    signed_in, context
) -> None:
    from mflbot.web import views

    view = views.team_view(context, week=WEEK + 9)  # a week with nothing stored
    assert view["roster"], "the fixture rosters players"
    assert all(row["projection_display"] == "--" for row in view["roster"])
    # A zero total would read as "projected to score nothing" rather than
    # "nothing is known", so there is no total at all.
    assert view["roster_projected"] is None

    body = signed_in.get(f"/team?week={WEEK + 9}").text
    assert 'class="num muted">--<' in body
    assert ">0.0<" not in body
    assert f"no week {WEEK + 9} projections stored" in body


def test_no_page_renders_a_credential(signed_in, context, monkeypatch) -> None:
    """The dashboard must never echo a secret, however it got into the process."""
    monkeypatch.setenv("MFLBOT_MFL_PASSWORD", "sentinel-password-value")
    monkeypatch.setenv("MFLBOT_MFL_API_KEY", "sentinel-api-key-value")
    context.client.auth.session_cookie = "sentinel-session-cookie"

    for path in ("/", "/recommendations", "/team", "/league", "/actions", "/audit",
                 "/api/state", f"/recommendations/{only_recommendation(context).id}"):
        body = signed_in.get(path).text
        for secret in ("sentinel-password-value", "sentinel-api-key-value",
                       "sentinel-session-cookie"):
            assert secret not in body, (path, secret)


def test_the_state_api_reports_what_is_blocked(signed_in, context) -> None:
    payload = signed_in.get("/api/state").json()
    assert payload["status"]["league_id"] == LEAGUE_ID
    assert payload["status"]["pending_count"] == 1
    assert payload["status"]["writes_total"] > 0
    assert "counts" in payload["status"]
