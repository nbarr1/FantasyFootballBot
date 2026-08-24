"""Player database ingestion.

MFL asks that the full player database be requested no more than once per day.
That limit is enforced by the endpoint's TTL in
:mod:`mflbot.mfl.endpoints`, so calling :func:`sync_players` more often than
that is harmless -- it re-reads the cache.
"""

from __future__ import annotations

import logging
from typing import Any

from ..analysis.rules_parser import mfl_text
from ..domain.models import Player
from ..errors import ParseError

log = logging.getLogger(__name__)


def parse_players(payload: Any) -> list[Player]:
    root = payload.get("players") if isinstance(payload, dict) else None
    if not isinstance(root, dict):
        raise ParseError("The players export did not contain a 'players' object")
    nodes = root.get("player", [])
    if isinstance(nodes, dict):
        nodes = [nodes]
    out: list[Player] = []
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        player_id = mfl_text(node.get("id"))
        name = mfl_text(node.get("name"))
        if not player_id or not name:
            continue
        out.append(
            Player(
                player_id=player_id,
                name=name,
                position=mfl_text(node.get("position")),
                nfl_team=mfl_text(node.get("team")),
                status=mfl_text(node.get("status")),
            )
        )
    return out


def sync_players(client, repos, *, force: bool = False) -> int:
    """Refresh the local player database. Returns the number of rows written."""
    payload = client.export("players", DETAILS="1", force_refresh=force).payload
    players = parse_players(payload)
    if not players:
        log.warning("The players export parsed to zero players; nothing was written")
        return 0
    return repos.upsert_players(players)
