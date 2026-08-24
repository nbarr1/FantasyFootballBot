"""Wiring news sources named in config to concrete adapters."""

from __future__ import annotations

import logging
from typing import Any, Callable, Sequence

from .base import NewsSource
from .mfl_injuries import MFLInjurySource
from .paid_stubs import FantasyNerdsSource, RotowireSource, SportsDataIOSource
from .sleeper import PlayerCrosswalk, SleeperSource

log = logging.getLogger(__name__)

#: source_id -> factory(client, repos, cache) -> NewsSource
SOURCE_REGISTRY: dict[str, Callable[..., NewsSource]] = {
    MFLInjurySource.source_id: lambda client, repos, cache: MFLInjurySource(client),
    SleeperSource.source_id: lambda client, repos, cache: SleeperSource(
        PlayerCrosswalk(_all_players(repos)), cache=cache
    ),
    SportsDataIOSource.source_id: lambda client, repos, cache: SportsDataIOSource(),
    FantasyNerdsSource.source_id: lambda client, repos, cache: FantasyNerdsSource(),
    RotowireSource.source_id: lambda client, repos, cache: RotowireSource(),
}


def _all_players(repos) -> list[Any]:
    rows = repos.db.query("SELECT * FROM players")
    from ...domain.models import Player

    return [
        Player(
            player_id=r["player_id"],
            name=r["name"],
            position=r["position"],
            nfl_team=r["nfl_team"],
            status=r["status"],
        )
        for r in rows
    ]


def build_sources(source_ids: Sequence[str], client, repos, cache=None) -> list[NewsSource]:
    """Instantiate the configured sources, skipping ones that cannot run."""
    sources: list[NewsSource] = []
    for source_id in source_ids:
        factory = SOURCE_REGISTRY.get(source_id)
        if factory is None:
            log.warning(
                "Unknown news source %r in config; known sources are %s",
                source_id,
                ", ".join(sorted(SOURCE_REGISTRY)),
            )
            continue
        source = factory(client, repos, cache)
        available, reason = source.is_available()
        if not available:
            log.warning("News source %r is unavailable: %s", source_id, reason)
            continue
        sources.append(source)
    return sources


def ingest_news(sources: Sequence[NewsSource], repos, player_ids=None) -> dict[str, int]:
    """Run each source and store what is new. Returns per-source new-item counts."""
    results: dict[str, int] = {}
    for source in sources:
        try:
            items = list(source.fetch(player_ids))
        except NotImplementedError as exc:
            log.info("Skipping %s: %s", source.source_id, exc)
            results[source.source_id] = 0
            continue
        except Exception as exc:  # noqa: BLE001 - a broken feed must be reported,
            # not silently treated as "no news", which would look like calm.
            log.error("News source %s failed: %s", source.source_id, exc)
            results[source.source_id] = -1
            continue
        new_items = repos.save_news(items)
        results[source.source_id] = len(new_items)
    return results
