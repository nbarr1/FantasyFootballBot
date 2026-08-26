"""The dashboard: a full interactive surface over the same bot the CLI drives.

What you can do here
--------------------

* See what is stored, what is blocked and why, and what the endpoint registry
  will and will not let fire.
* Run ingestion, analysis and verification, watching the output stream live.
* Read every recommendation in full -- rationale, evidence, caveats and the
  literal payload -- then approve, reject, or edit it.
* Submit an approved action to MFL, as a separate, deliberate second click.
* Read the audit trail of every write ever attempted.

What it does not change
-----------------------

The safety invariants are enforced below this layer, and this layer is written
so it cannot route around them:

* Approval goes through the same :class:`~mflbot.approval.token.TokenService`
  as the CLI, via :class:`~mflbot.approval.web_channel.WebApprovalChannel`,
  which delegates to the CLI channel rather than reimplementing it.
* There is no "approve all". Every route acts on exactly one recommendation id.
* Editing changes the payload hash, so a token minted before the edit stops
  matching and the submit button refuses.
* The action buttons run allowlisted read/analysis commands only (see
  :mod:`mflbot.web.jobs`); none of them can submit anything.
* Nothing here reads a credential. The only page that takes one is the login
  form, and what it compares against is a hash held in memory.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from ..approval.web_channel import WebApprovalChannel
from ..errors import MFLBotError
from . import views
from .events import EventBus, drain
from .jobs import JOB_SPECS, JobManager
from .security import (
    CSRF_FIELD,
    CSRF_HEADER,
    SESSION_COOKIE,
    Session,
    WebAuthError,
    WebSecurity,
    origin_is_allowed,
)

#: FastAPI is an optional extra, but its names have to live at module scope:
#: FastAPI resolves each handler's annotations with ``get_type_hints``, which
#: searches module globals. Imported inside ``build_app`` instead, ``Request``
#: would not resolve and every handler would treat it as a query parameter.
#: Import failure is recorded rather than raised, so ``import mflbot.web``
#: still works without the extra and ``build_app`` is what explains the fix.
try:
    from fastapi import FastAPI, Form, HTTPException, Request
    from fastapi.responses import (
        HTMLResponse,
        JSONResponse,
        RedirectResponse,
        StreamingResponse,
    )
    from fastapi.staticfiles import StaticFiles
    from fastapi.templating import Jinja2Templates
    from starlette.exceptions import HTTPException as StarletteHTTPException

    _IMPORT_ERROR: ImportError | None = None
except ImportError as exc:  # pragma: no cover - dependency guard
    _IMPORT_ERROR = exc

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

#: Reachable without a session. Deliberately tiny, and none of it discloses
#: league data: /healthz answers "the process is up" and nothing else.
PUBLIC_PATHS = frozenset({"/login", "/healthz"})

MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def build_app(
    context,
    security: WebSecurity,
    *,
    allow_submissions: bool = True,
    allow_jobs: bool = True,
    start_scheduler: bool = False,
    scheduler_factory=None,
):
    """Construct the FastAPI application.

    ``allow_submissions=False`` runs the dashboard as a read-and-decide
    surface: approvals are still recorded and tokens still minted, but no web
    request can cause an MFL write -- ``bot execute`` picks them up instead.
    That split exists for anyone who does not want a browser tab to be able to
    submit at all.

    ``allow_jobs=False`` additionally removes the buttons that run ingestion
    and analysis, leaving a viewer over whatever the scheduler produced.

    ``start_scheduler=True`` starts the background scheduler with the server,
    so one process both watches the league and serves the dashboard. That is
    the deployment shape for an always-on host: two processes against one
    SQLite file would contend for its write lock.
    """
    if _IMPORT_ERROR is not None:  # pragma: no cover - dependency guard
        raise ImportError(
            "The dashboard needs the 'web' extra: pip install 'mflbot[web]'"
        ) from _IMPORT_ERROR

    bus = EventBus()
    jobs = JobManager(context, bus, enabled=allow_jobs)
    channel = WebApprovalChannel(context.store, context.tokens, context.notifier)
    flashes: dict[str, list[dict[str, str]]] = defaultdict(list)
    scheduler_state: dict[str, Any] = {"scheduler": None}

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        if start_scheduler and allow_jobs:
            _start_scheduler()
            log.info("scheduler started with the dashboard")
        yield
        # Stop the worker thread and any scheduler this process started, so
        # Ctrl-C actually ends the process rather than leaving threads behind.
        jobs.stop()
        _stop_scheduler()

    app = FastAPI(
        title="mflbot",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.state.context = context
    app.state.security = security
    app.state.jobs = jobs
    app.state.bus = bus
    app.state.allow_submissions = allow_submissions

    # -- small helpers -----------------------------------------------------

    def flash(session: Session, message: str, tone: str = "info") -> None:
        flashes[session.id].append({"message": message, "tone": tone})

    def take_flashes(session: Session) -> list[dict[str, str]]:
        return flashes.pop(session.id, [])

    def session_of(request: Request) -> Session:
        session = getattr(request.state, "session", None)
        if session is None:  # pragma: no cover - the guard runs first
            raise HTTPException(status_code=401, detail="not authenticated")
        return session

    def check_csrf(request: Request, submitted: str | None) -> None:
        session = session_of(request)
        token = submitted or request.headers.get(CSRF_HEADER)
        if not security.check_csrf(session, token):
            log.warning("CSRF check failed for %s %s", request.method, request.url.path)
            raise HTTPException(
                status_code=403,
                detail="This form was not submitted from the dashboard, or your "
                       "session changed. Reload the page and try again.",
            )

    def page(request: Request, name: str, **ctx: Any):
        session = session_of(request)
        return templates.TemplateResponse(
            request=request,
            name=name,
            context={
                "status": views.status_view(
                    context, jobs=jobs, submissions_enabled=allow_submissions
                ),
                "csrf_token": session.csrf_token,
                "flashes": take_flashes(session),
                "allow_jobs": allow_jobs,
                "allow_submissions": allow_submissions,
                "path": request.url.path,
                "now": datetime.now(UTC),
                **ctx,
            },
        )

    def safe_path(candidate: str | None, default: str) -> str:
        """A redirect target that cannot leave this site.

        Only same-site absolute *paths* are honoured; a URL with a scheme or a
        host -- including the protocol-relative ``//evil.example`` -- would
        otherwise turn this dashboard into an open redirect that a phishing
        link could aim anywhere.
        """
        candidate = candidate or default
        parsed = urlsplit(candidate)
        if parsed.scheme or parsed.netloc or not candidate.startswith("/"):
            return default
        return candidate

    def back_to(request: Request, default: str) -> str:
        """Where to send the browser after a form post."""
        return safe_path(request.query_params.get("next"), default)

    def wants_json(request: Request) -> bool:
        return request.url.path.startswith("/api/") or "application/json" in (
            request.headers.get("accept") or ""
        )

    # -- the guard ---------------------------------------------------------

    @app.middleware("http")
    async def guard(request: Request, call_next):
        """Authenticate every request, and reject cross-site mutations.

        Runs before routing, so a route added later is protected by default
        rather than by remembering to decorate it. The CSRF *token* is checked
        inside each handler (the body is not read here); what this does is the
        Origin check, which needs no body.
        """
        path = request.url.path
        if path in PUBLIC_PATHS or path.startswith("/static/"):
            return await call_next(request)

        session = security.session_for(request.cookies.get(SESSION_COOKIE))
        if session is None:
            if wants_json(request):
                return JSONResponse(
                    {"error": "authentication required"}, status_code=401
                )
            return RedirectResponse(f"/login?next={quote(path)}", status_code=303)

        if request.method in MUTATING_METHODS and not origin_is_allowed(
            request.headers.get("origin"), request.headers.get("host")
        ):
            log.warning("cross-site %s to %s refused", request.method, path)
            return JSONResponse({"error": "cross-site request refused"}, status_code=403)

        request.state.session = session
        return await call_next(request)

    # -- login -------------------------------------------------------------

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        if security.session_for(request.cookies.get(SESSION_COOKIE)):
            return RedirectResponse("/", status_code=303)
        token = request.query_params.get("token")
        if token:
            # The startup URL carries the access token. Exchanging it here for
            # a session cookie means the token stops travelling with every
            # subsequent request, and the address bar keeps no copy of it.
            try:
                session = security.login(token)
            except WebAuthError as exc:
                return templates.TemplateResponse(
                    request=request,
                    name="login.html",
                    context={"error": str(exc), "mode": security.mode},
                    status_code=401,
                )
            response = RedirectResponse(back_to(request, "/"), status_code=303)
            _set_session_cookie(response, session, request)
            return response
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": None, "mode": security.mode},
        )

    @app.post("/login", response_class=HTMLResponse)
    def login(
        request: Request,
        secret: str = Form(""),
        next_path: str = Form("/", alias="next"),
    ):
        if not origin_is_allowed(
            request.headers.get("origin"), request.headers.get("host")
        ):
            raise HTTPException(status_code=403, detail="cross-site login refused")
        try:
            session = security.login(secret)
        except WebAuthError as exc:
            return templates.TemplateResponse(
                request=request,
                name="login.html",
                context={"error": str(exc), "mode": security.mode},
                status_code=401,
            )
        response = RedirectResponse(safe_path(next_path, "/"), status_code=303)
        _set_session_cookie(response, session, request)
        return response

    @app.post("/logout")
    def logout(request: Request, csrf_token: str = Form("")):
        check_csrf(request, csrf_token)
        session = session_of(request)
        flashes.pop(session.id, None)
        security.logout(session.id)
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    def _set_session_cookie(response, session: Session, request: Request) -> None:
        response.set_cookie(
            SESSION_COOKIE,
            session.id,
            httponly=True,
            samesite="lax",
            # Only mark Secure when the connection actually is TLS: a Secure
            # cookie over plain http://127.0.0.1 is silently dropped, which
            # would present as "login does nothing".
            secure=request.url.scheme == "https",
            path="/",
            max_age=int(security.session_ttl.total_seconds()),
        )

    # -- pages -------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        context.store.expire_stale()
        pending = context.store.pending()
        names = views.player_names(context, views.recommendation_ids(pending))
        return page(
            request,
            "dashboard.html",
            pending=[views.recommendation_view(r, names) for r in pending],
            recent=[
                views.recommendation_view(r)
                for r in context.store.all(limit=8)
                if r.status != "proposed"
            ][:5],
            specs=[spec.as_dict() for spec in JOB_SPECS],
            runs=[run.as_dict(include_output=False) for run in jobs.runs(limit=6)],
            league=views.league_view(context),
        )

    @app.get("/recommendations", response_class=HTMLResponse)
    def recommendation_list(request: Request):
        context.store.expire_stale()
        wanted = request.query_params.get("status", "").strip()
        records = context.store.all(limit=200)
        if wanted:
            records = [r for r in records if str(r.status) == wanted]
        names = views.player_names(context, views.recommendation_ids(records))
        return page(
            request,
            "recommendations.html",
            recommendations=[views.recommendation_view(r, names) for r in records],
            selected_status=wanted,
            statuses=["proposed", "approved", "executed", "rejected", "expired", "failed"],
        )

    @app.get("/recommendations/{recommendation_id}", response_class=HTMLResponse)
    def recommendation_detail(request: Request, recommendation_id: str):
        recommendation = context.store.get(recommendation_id)
        if recommendation is None:
            raise HTTPException(status_code=404, detail="No such recommendation.")
        names = views.player_names(context, views.recommendation_ids([recommendation]))
        token = context.tokens.latest_for(recommendation_id)
        return page(
            request,
            "detail.html",
            recommendation=views.recommendation_view(recommendation, names),
            token_id=token.token_id if token else None,
            token_expires=views.humanise_delta(token.expires_at) if token else None,
            token_expires_iso=token.expires_at.isoformat() if token else "",
        )

    @app.get("/league", response_class=HTMLResponse)
    def league(request: Request):
        return page(request, "league.html", league=views.league_view(context))

    @app.get("/team", response_class=HTMLResponse)
    def team(request: Request):
        week = request.query_params.get("week")
        return page(
            request,
            "team.html",
            team=views.team_view(context, int(week) if week and week.isdigit() else None),
        )

    @app.get("/audit", response_class=HTMLResponse)
    def audit(request: Request):
        return page(request, "audit.html", entries=views.audit_view(context, limit=200))

    @app.get("/actions", response_class=HTMLResponse)
    def actions(request: Request):
        scheduler = scheduler_state["scheduler"]
        return page(
            request,
            "actions.html",
            specs=[spec.as_dict() for spec in JOB_SPECS],
            runs=[run.as_dict(include_output=False) for run in jobs.runs()],
            scheduler_running=bool(scheduler and scheduler.running),
            scheduler_jobs=_scheduler_jobs(scheduler),
        )

    # -- decisions ---------------------------------------------------------

    @app.post("/recommendations/{recommendation_id}/approve")
    def approve(
        request: Request,
        recommendation_id: str,
        csrf_token: str = Form(""),
        submit_now: str = Form(""),
    ):
        check_csrf(request, csrf_token)
        session = session_of(request)
        actor = f"web:{session.actor}"
        target = f"/recommendations/{recommendation_id}"
        try:
            decision = channel.approve(recommendation_id, actor=actor)
        except MFLBotError as exc:
            flash(session, str(exc), "bad")
            return RedirectResponse(target, status_code=303)

        flash(
            session,
            f"Approved {recommendation_id}. The token is bound to this exact "
            f"payload and is valid once.",
            "good",
        )
        if submit_now and decision.token is not None:
            _submit(session, recommendation_id, decision.token)
        elif decision.token is not None and allow_submissions:
            flash(session, "Nothing has been sent to MFL yet -- submit when ready.",
                  "info")
        bus.publish("state.changed", reason="approve")
        return RedirectResponse(target, status_code=303)

    @app.post("/recommendations/{recommendation_id}/submit")
    def submit(request: Request, recommendation_id: str, csrf_token: str = Form("")):
        check_csrf(request, csrf_token)
        session = session_of(request)
        target = f"/recommendations/{recommendation_id}"
        token = context.tokens.latest_for(recommendation_id)
        if token is None:
            flash(
                session,
                "There is no live approval for this action. Approve it first; an "
                "approval is what authorises a submission.",
                "bad",
            )
            return RedirectResponse(target, status_code=303)
        _submit(session, recommendation_id, token)
        bus.publish("state.changed", reason="submit")
        return RedirectResponse(target, status_code=303)

    def _submit(session: Session, recommendation_id: str, token) -> None:
        """The one path from a browser to an MFL write."""
        if not allow_submissions:
            flash(
                session,
                "This dashboard was started with --no-submit. The approval is "
                "recorded; run `bot execute` to submit it.",
                "info",
            )
            return
        recommendation = context.store.get(recommendation_id)
        if recommendation is None:  # pragma: no cover - approved then deleted
            flash(session, "That recommendation no longer exists.", "bad")
            return
        try:
            outcome = context.execute_approved(recommendation, token)
        except MFLBotError as exc:
            flash(session, f"Not submitted: {exc}", "bad")
            return
        flash(session, outcome.message, "good" if outcome.ok else "bad")

    @app.post("/recommendations/{recommendation_id}/reject")
    def reject(
        request: Request,
        recommendation_id: str,
        csrf_token: str = Form(""),
        note: str = Form(""),
    ):
        check_csrf(request, csrf_token)
        session = session_of(request)
        try:
            channel.reject(recommendation_id, actor=f"web:{session.actor}", note=note)
        except MFLBotError as exc:
            flash(session, str(exc), "bad")
        else:
            flash(session, f"Rejected {recommendation_id}. Nothing was submitted.",
                  "info")
        bus.publish("state.changed", reason="reject")
        return RedirectResponse(back_to(request, "/recommendations"), status_code=303)

    @app.post("/recommendations/{recommendation_id}/edit")
    async def edit(request: Request, recommendation_id: str):
        form = await request.form()
        check_csrf(request, str(form.get(CSRF_FIELD, "")))
        session = session_of(request)
        target = f"/recommendations/{recommendation_id}"
        recommendation = context.store.get(recommendation_id)
        if recommendation is None:
            raise HTTPException(status_code=404, detail="No such recommendation.")

        # Only fields the form actually carried, and only those whose value
        # changed, are passed on: an untouched form is a no-op rather than a
        # payload rewrite that would invalidate an approval for no reason, and
        # a field the form never mentioned is not an instruction to clear it.
        changes: dict[str, str] = {}
        for field in views.editable_fields(recommendation):
            key = f"field_{field['name']}"
            if key not in form:
                continue
            submitted = str(form.get(key, "")).strip()
            if submitted == field["value"].strip():
                continue
            if field["is_list"] and not submitted and field["value"]:
                flash(
                    session,
                    f"{field['label']} cannot be emptied -- an action with nothing "
                    f"in it is not an action. Reject this recommendation instead.",
                    "bad",
                )
                return RedirectResponse(target, status_code=303)
            changes[field["name"]] = submitted
        if not changes:
            flash(session, "Nothing changed.", "info")
            return RedirectResponse(target, status_code=303)
        try:
            decision = channel.edit(
                recommendation_id, changes, actor=f"web:{session.actor}"
            )
        except (MFLBotError, ValueError) as exc:
            flash(session, f"Edit refused: {exc}", "bad")
            return RedirectResponse(target, status_code=303)
        flash(session, f"Edited {', '.join(sorted(changes))}. {decision.note}", "good")
        bus.publish("state.changed", reason="edit")
        return RedirectResponse(target, status_code=303)

    # -- jobs and scheduler ------------------------------------------------

    @app.post("/actions/{spec_id}/run")
    async def run_action(request: Request, spec_id: str):
        form = await request.form()
        check_csrf(request, str(form.get(CSRF_FIELD, "")))
        session = session_of(request)
        params = {
            key.removeprefix("param_"): str(value)
            for key, value in form.items()
            if key.startswith("param_")
        }
        try:
            run = jobs.submit(spec_id, params, actor=session.actor)
        except KeyError:
            raise HTTPException(status_code=404, detail="No such action.") from None
        except (ValueError, PermissionError) as exc:
            flash(session, str(exc), "bad")
            return RedirectResponse(back_to(request, "/actions"), status_code=303)
        flash(session, f"Started: {run.command}", "info")
        return RedirectResponse(
            back_to(request, f"/actions#run-{run.id}"), status_code=303
        )

    @app.post("/scheduler/{action}")
    def scheduler_control(request: Request, action: str, csrf_token: str = Form("")):
        check_csrf(request, csrf_token)
        session = session_of(request)
        if not allow_jobs:
            flash(session, "This dashboard was started with --no-jobs.", "bad")
            return RedirectResponse("/actions", status_code=303)
        if action not in {"start", "stop"}:
            raise HTTPException(status_code=404, detail="No such scheduler action.")
        try:
            if action == "start":
                _start_scheduler()
                flash(session, "Scheduler started. It polls and analyses; it never "
                               "submits.", "good")
            else:
                _stop_scheduler()
                flash(session, "Scheduler stopped.", "info")
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            flash(session, f"Scheduler {action} failed: {exc}", "bad")
        bus.publish("state.changed", reason=f"scheduler:{action}")
        return RedirectResponse("/actions", status_code=303)

    def _start_scheduler() -> None:
        if scheduler_state["scheduler"] is not None:
            return
        if scheduler_factory is not None:
            scheduler = scheduler_factory(context)
        else:
            from ..schedule.jobs import JobRunner, build_scheduler

            scheduler = build_scheduler(JobRunner(context), context.config)
        scheduler.start()
        scheduler_state["scheduler"] = scheduler

    def _stop_scheduler() -> None:
        scheduler = scheduler_state["scheduler"]
        if scheduler is not None:
            scheduler.shutdown(wait=False)
            scheduler_state["scheduler"] = None

    def _scheduler_jobs(scheduler) -> list[dict[str, str]]:
        if scheduler is None or not scheduler.running:
            return []
        return [
            {
                "id": job.id,
                "next_run": job.next_run_time.strftime("%Y-%m-%d %H:%M UTC")
                if job.next_run_time
                else "not scheduled",
            }
            for job in scheduler.get_jobs()
        ]

    # -- JSON and the live stream ------------------------------------------

    @app.get("/api/state")
    def api_state(request: Request):
        return JSONResponse(
            {
                "status": views.status_view(
                    context, jobs=jobs, submissions_enabled=allow_submissions
                ),
                "runs": [r.as_dict(include_output=False) for r in jobs.runs(limit=10)],
            }
        )

    @app.get("/api/recommendations")
    def api_recommendations(request: Request):
        context.store.expire_stale()
        records = context.store.all(limit=100)
        names = views.player_names(context, views.recommendation_ids(records))
        return JSONResponse(
            {"recommendations": [views.recommendation_view(r, names) for r in records]}
        )

    @app.get("/api/runs/{run_id}")
    def api_run(request: Request, run_id: str):
        run = jobs.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="No such run.")
        return JSONResponse(run.as_dict())

    @app.get("/events")
    async def events(request: Request):
        """Server-sent events: job output and "go re-read the state" nudges.

        Polling every queue with a short sleep rather than blocking on it keeps
        the event loop free; a browser tab costs a subscription and no thread.
        """

        async def stream():
            subscription = bus.subscribe()
            try:
                yield ": connected\n\n"
                idle = 0.0
                while True:
                    if await request.is_disconnected():
                        return
                    batch = drain(subscription)
                    for event in batch:
                        yield event.sse()
                    if batch:
                        idle = 0.0
                    else:
                        idle += 0.25
                        if idle >= 15.0:  # keep proxies and browsers from timing out
                            idle = 0.0
                            yield ": heartbeat\n\n"
                    await asyncio.sleep(0.25)
            finally:
                subscription.close()

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.get("/healthz")
    def healthz():
        """Liveness only. Says nothing about the league, and needs no session."""
        return JSONResponse({"status": "ok"})

    # -- error rendering ---------------------------------------------------

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        """Render refusals as a readable page, with the reason intact.

        The reason matters here more than usual: "your session changed, reload"
        and "that recommendation does not exist" lead to different next moves,
        and a bare 403 tells the user neither.
        """
        if wants_json(request):
            return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
        session = security.session_for(request.cookies.get(SESSION_COOKIE))
        return templates.TemplateResponse(
            request=request,
            name="error.html",
            context={
                "code": exc.status_code,
                "message": exc.detail,
                "status": None,
                "csrf_token": session.csrf_token if session else "",
                "flashes": [],
                "allow_jobs": allow_jobs,
                "allow_submissions": allow_submissions,
                "path": request.url.path,
            },
            status_code=exc.status_code,
        )

    return app
