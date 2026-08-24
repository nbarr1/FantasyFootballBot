"""FastAPI dashboard for reviewing and approving recommendations.

Bound to localhost by default. Approval here goes through exactly the same
:class:`~mflbot.approval.token.TokenService` as the CLI, so the same invariants
hold: one token per recommendation, bound to the payload, single-use.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path

from ...errors import ApprovalError
from ...recommend.models import Recommendation
from ..channel import ApprovalChannel, ApprovalDecision
from ..cli_channel import CLIApprovalChannel

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"


class WebApprovalChannel(ApprovalChannel):
    """Dashboard-backed channel.

    Decision logic is delegated to :class:`CLIApprovalChannel` rather than
    duplicated: both surfaces must enforce the same rules, and one
    implementation cannot drift from itself.
    """

    channel_id = "web"

    def __init__(self, store, tokens, notifier=None) -> None:
        self._inner = CLIApprovalChannel(store, tokens, notifier, writer=lambda _: None)
        self._store = store

    def present(self, recommendations: Sequence[Recommendation]) -> None:
        """No-op: the dashboard renders on request rather than being pushed to."""
        return None

    def approve(self, recommendation_id: str, *, actor: str = "web") -> ApprovalDecision:
        return self._inner.approve(recommendation_id, actor=actor)

    def reject(
        self, recommendation_id: str, *, actor: str = "web", note: str = ""
    ) -> ApprovalDecision:
        return self._inner.reject(recommendation_id, actor=actor, note=note)

    def edit(
        self, recommendation_id: str, changes: dict, *, actor: str = "web"
    ) -> ApprovalDecision:
        return self._inner.edit(recommendation_id, changes, actor=actor)


def build_app(store, tokens, executor=None, notifier=None):
    """Construct the FastAPI application.

    ``executor`` is optional. When absent the dashboard records approvals but
    does not submit them -- ``bot execute`` picks them up instead. That split
    exists so the dashboard can be run by someone who does not want a web
    request to trigger an MFL write at all.
    """
    try:
        from fastapi import FastAPI, Form, Request
        from fastapi.responses import HTMLResponse, RedirectResponse
        from fastapi.templating import Jinja2Templates
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "The web dashboard needs the 'web' extra: pip install 'mflbot[web]'"
        ) from exc

    app = FastAPI(title="mflbot approvals", docs_url=None, redoc_url=None)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    channel = WebApprovalChannel(store, tokens, notifier)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        store.expire_stale()
        return templates.TemplateResponse(
            "pending.html",
            {
                "request": request,
                "recommendations": store.pending(),
                "recent": store.all(limit=20),
            },
        )

    @app.get("/recommendation/{recommendation_id}", response_class=HTMLResponse)
    def detail(request: Request, recommendation_id: str):
        recommendation = store.get(recommendation_id)
        return templates.TemplateResponse(
            "detail.html",
            {"request": request, "recommendation": recommendation, "error": None},
        )

    @app.post("/recommendation/{recommendation_id}/approve")
    def approve(recommendation_id: str):
        try:
            decision = channel.approve(recommendation_id)
        except ApprovalError as exc:
            log.warning("approval refused: %s", exc)
            return RedirectResponse(f"/recommendation/{recommendation_id}", status_code=303)
        if executor is not None and decision.token is not None:
            recommendation = store.get(recommendation_id)
            executor.execute(recommendation, decision.token)
        return RedirectResponse("/", status_code=303)

    @app.post("/recommendation/{recommendation_id}/reject")
    def reject(recommendation_id: str, note: str = Form("")):
        channel.reject(recommendation_id, note=note)
        return RedirectResponse("/", status_code=303)

    @app.post("/recommendation/{recommendation_id}/edit")
    def edit(recommendation_id: str, changes: str = Form("")):
        parsed = {}
        for pair in changes.split():
            if "=" in pair:
                key, _, value = pair.partition("=")
                parsed[key.strip()] = value.strip()
        try:
            channel.edit(recommendation_id, parsed)
        except ApprovalError as exc:
            log.warning("edit refused: %s", exc)
        return RedirectResponse(f"/recommendation/{recommendation_id}", status_code=303)

    return app
