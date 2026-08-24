"""MFL's own injury feed -- the always-on news baseline.

This source is authoritative for official designations (Out, Doubtful,
Questionable, IR) for exactly the players in this league, and it needs no extra
credentials beyond the MFL client the bot already has.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime

from ...analysis.rules_parser import mfl_text
from ...domain.models import NewsItem
from .base import Classification, NewsSource


class MFLInjurySource(NewsSource):
    source_id = "mfl_injuries"

    def __init__(self, client) -> None:
        self._client = client

    def fetch(self, player_ids: Sequence[str] | None = None) -> Iterable[NewsItem]:
        payload = self._client.export("injuries").payload
        root = payload.get("injuries") if isinstance(payload, dict) else None
        if not isinstance(root, dict):
            return []

        week = mfl_text(root.get("week")) or ""
        timestamp = mfl_text(root.get("timestamp"))
        published = _epoch(timestamp)

        nodes = root.get("injury", [])
        if isinstance(nodes, dict):
            nodes = [nodes]

        wanted = set(player_ids) if player_ids else None
        items: list[NewsItem] = []
        for node in nodes or []:
            if not isinstance(node, dict):
                continue
            pid = mfl_text(node.get("id"))
            if not pid or (wanted is not None and pid not in wanted):
                continue
            status = mfl_text(node.get("status")) or "unknown"
            details = mfl_text(node.get("details")) or ""
            items.append(
                NewsItem(
                    source=self.source_id,
                    # Identity includes the status, so a change of designation
                    # registers as new news rather than being deduplicated away.
                    external_id=f"{week}:{pid}:{status}",
                    player_id=pid,
                    player_name=None,
                    published_at=published,
                    classification=Classification.INJURY,
                    headline=f"Injury designation: {status}",
                    body=details,
                    url=None,
                )
            )
        return items


def _epoch(raw: str | None) -> datetime | None:
    if not raw or not raw.strip().isdigit():
        return None
    try:
        return datetime.fromtimestamp(int(raw), tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None
