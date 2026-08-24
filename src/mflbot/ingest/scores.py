"""Actual-score and projection ingestion."""

from __future__ import annotations

import logging
from typing import Any

from ..analysis.rules_parser import mfl_text
from ..domain.models import Projection
from ..errors import ParseError

log = logging.getLogger(__name__)

MFL_PROJECTION_SOURCE = "mfl_projectedScores"


def _score_nodes(payload: Any, root_key: str) -> list[dict[str, Any]]:
    root = payload.get(root_key) if isinstance(payload, dict) else None
    if not isinstance(root, dict):
        raise ParseError(f"Score export did not contain a '{root_key}' object")
    nodes = root.get("playerScore", root.get("player", []))
    if isinstance(nodes, dict):
        nodes = [nodes]
    return [n for n in (nodes or []) if isinstance(n, dict)]


def parse_player_scores(payload: Any) -> list[tuple[str, float]]:
    out: list[tuple[str, float]] = []
    for node in _score_nodes(payload, "playerScores"):
        pid = mfl_text(node.get("id"))
        raw = mfl_text(node.get("score"))
        if not pid or raw is None:
            continue
        try:
            out.append((pid, float(raw)))
        except ValueError:
            continue
    return out


def parse_projected_scores(payload: Any, week: int) -> list[Projection]:
    out: list[Projection] = []
    for node in _score_nodes(payload, "projectedScores"):
        pid = mfl_text(node.get("id"))
        raw = mfl_text(node.get("score"))
        if not pid or raw is None:
            continue
        try:
            points = float(raw)
        except ValueError:
            continue
        out.append(Projection(pid, week, MFL_PROJECTION_SOURCE, points))
    return out


def sync_scores(client, repos, week: int, *, is_final: bool = False, force: bool = False) -> int:
    payload = client.export(
        "playerScores", L=client.league.id, W=week, force_refresh=force
    ).payload
    scores = parse_player_scores(payload)
    return repos.save_scores(
        client.league.id, client.league.season, week, scores, is_final=is_final
    )


def sync_projections(client, repos, week: int, *, force: bool = False) -> int:
    """Ingest MFL's own projections for a week.

    Returns 0 when MFL has published none for this league's host. That is a
    real, reportable state: the projection-dependent engines then block rather
    than inventing numbers.
    """
    payload = client.export(
        "projectedScores", L=client.league.id, W=week, force_refresh=force
    ).payload
    projections = parse_projected_scores(payload, week)
    if not projections:
        log.warning("MFL published no projections for week %s on this host", week)
        return 0
    return repos.save_projections(client.league.id, client.league.season, projections)
