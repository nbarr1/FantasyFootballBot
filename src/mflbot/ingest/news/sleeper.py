"""Sleeper public API adapter -- the default external news source.

Chosen as the default because it needs no account, no API key and no scraping:
it is a public JSON API. It contributes two things MFL's own feed does not:

* a richer per-player status/injury note (including practice participation and
  short injury descriptions), and
* *trending adds and drops* across Sleeper's whole user base, which is a useful
  early signal that the wider market has noticed a usage change -- often a day
  before it shows up in this league's own transaction log.

Sleeper uses its own player ids, so every item is crosswalked to an MFL player
id through :class:`PlayerCrosswalk` before it is stored. An item that cannot be
matched to a player in *this* league's database is dropped rather than stored
against a guessed id.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

from ...domain.models import NewsItem, Player
from .base import Classification, NewsSource

log = logging.getLogger(__name__)

SLEEPER_BASE = "https://api.sleeper.app/v1"
#: Sleeper's own guidance is to pull the full player file at most once a day.
PLAYERS_TTL_SECONDS = 86_400
TRENDING_TTL_SECONDS = 3_600

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}
#: Dropped outright rather than replaced with a space, so that "D'Andre" and
#: "DAndre" -- both of which occur across providers -- fold to the same key.
_ELIDED = re.compile(r"['\u2019.]+")
_NON_ALPHA = re.compile(r"[^a-z ]+")


def normalise_name(name: str) -> str:
    """Fold a player name to a comparable key.

    Handles the differences that actually occur between provider name spellings:
    accents, punctuation in names like ``D'Andre``, and generational suffixes.
    """
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    folded = _ELIDED.sub("", folded.lower())
    folded = _NON_ALPHA.sub(" ", folded)
    parts = [p for p in folded.split() if p and p not in _SUFFIXES]
    return " ".join(parts)


class PlayerCrosswalk:
    """Maps an external provider's player identity onto MFL player ids.

    Matching is deliberately conservative. A name alone is ambiguous, so a match
    requires the name plus at least one corroborating attribute (position or NFL
    team). Ambiguous names that cannot be disambiguated are reported as misses
    rather than resolved by picking the first candidate.
    """

    def __init__(self, players: Iterable[Player]) -> None:
        self._by_name: dict[str, list[Player]] = {}
        for player in players:
            self._by_name.setdefault(normalise_name(player.name), []).append(player)

    def resolve(
        self, name: str, position: str | None = None, nfl_team: str | None = None
    ) -> str | None:
        candidates = self._by_name.get(normalise_name(name), [])
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0].player_id
        narrowed = candidates
        if position:
            narrowed = [c for c in narrowed if c.position == position] or narrowed
        if nfl_team:
            narrowed = [c for c in narrowed if c.nfl_team == nfl_team] or narrowed
        if len(narrowed) == 1:
            return narrowed[0].player_id
        log.debug("Ambiguous crosswalk for %r (%s candidates)", name, len(narrowed))
        return None


class SleeperSource(NewsSource):
    source_id = "sleeper"
    requires_credentials = False

    def __init__(self, crosswalk: PlayerCrosswalk, *, http=None, cache=None,
                 trending_limit: int = 25, lookback_hours: int = 24) -> None:
        self._crosswalk = crosswalk
        self._cache = cache
        self._trending_limit = trending_limit
        self._lookback_hours = lookback_hours
        if http is not None:
            self._http = http
            self._owns_http = False
        else:
            import httpx

            self._http = httpx.Client(
                timeout=30.0, headers={"User-Agent": "mflbot/0.1"}
            )
            self._owns_http = True

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def _get_json(self, path: str, cache_key: str, ttl: int) -> Any:
        if self._cache is not None:
            hit = self._cache.get(cache_key)
            if hit is not None:
                return hit.payload
        response = self._http.get(f"{SLEEPER_BASE}{path}")
        response.raise_for_status()
        payload = response.json()
        if self._cache is not None:
            self._cache.put(cache_key, payload, ttl)
        return payload

    def _players(self) -> dict[str, Any]:
        payload = self._get_json("/players/nfl", "sleeper-players-nfl", PLAYERS_TTL_SECONDS)
        return payload if isinstance(payload, dict) else {}

    def fetch(self, player_ids: Sequence[str] | None = None) -> Iterable[NewsItem]:
        catalogue = self._players()
        if not catalogue:
            return []

        wanted = set(player_ids) if player_ids else None
        now = datetime.now(UTC)
        items: list[NewsItem] = []

        # 1. Injury / status notes, crosswalked to MFL ids.
        for sleeper_id, record in catalogue.items():
            if not isinstance(record, dict):
                continue
            status = record.get("injury_status")
            if not status:
                continue
            full_name = record.get("full_name") or " ".join(
                filter(None, [record.get("first_name"), record.get("last_name")])
            )
            if not full_name:
                continue
            mfl_id = self._crosswalk.resolve(
                full_name, record.get("position"), record.get("team")
            )
            if not mfl_id or (wanted is not None and mfl_id not in wanted):
                continue
            body_bits = [
                str(record.get(key))
                for key in ("injury_body_part", "injury_notes", "practice_participation")
                if record.get(key)
            ]
            items.append(
                NewsItem(
                    source=self.source_id,
                    external_id=f"status:{sleeper_id}:{status}",
                    player_id=mfl_id,
                    player_name=full_name,
                    published_at=now,
                    classification=Classification.INJURY,
                    headline=f"Sleeper status: {status}",
                    body="; ".join(body_bits),
                    url=None,
                )
            )

        # 2. Market movement: trending adds and drops.
        for direction, classification in (
            ("add", Classification.USAGE),
            ("drop", Classification.USAGE),
        ):
            try:
                trending = self._get_json(
                    f"/players/nfl/trending/{direction}"
                    f"?lookback_hours={self._lookback_hours}&limit={self._trending_limit}",
                    f"sleeper-trending-{direction}-{self._lookback_hours}-{self._trending_limit}",
                    TRENDING_TTL_SECONDS,
                )
            except Exception as exc:  # noqa: BLE001 - one feed failing is not fatal
                log.warning("Sleeper trending/%s unavailable: %s", direction, exc)
                continue
            for entry in trending or []:
                if not isinstance(entry, dict):
                    continue
                record = catalogue.get(str(entry.get("player_id")))
                if not isinstance(record, dict):
                    continue
                full_name = record.get("full_name") or " ".join(
                    filter(None, [record.get("first_name"), record.get("last_name")])
                )
                if not full_name:
                    continue
                mfl_id = self._crosswalk.resolve(
                    full_name, record.get("position"), record.get("team")
                )
                if not mfl_id or (wanted is not None and mfl_id not in wanted):
                    continue
                count = entry.get("count")
                items.append(
                    NewsItem(
                        source=self.source_id,
                        external_id=f"trending:{direction}:{entry.get('player_id')}:"
                                    f"{now:%Y-%m-%dT%H}",
                        player_id=mfl_id,
                        player_name=full_name,
                        published_at=now,
                        classification=classification,
                        headline=f"Trending {direction} across Sleeper "
                                 f"({count} in {self._lookback_hours}h)",
                        body="Market interest signal, not a usage fact. Confirm the "
                             "underlying reason before acting on it.",
                        url=None,
                    )
                )
        return items
