"""Response cache with per-endpoint TTLs.

Caching is a *client* concern here, not a caller concern. Analysis code may ask
for the player database repeatedly within a run; MFL sees one request per day.
The TTL for each endpoint is declared once, in
:mod:`mflbot.mfl.endpoints`.

Entries are stored on disk so that TTLs survive process restarts -- a scheduler
that restarts hourly must not re-download the player database each time.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def cache_key(type_name: str, params: dict[str, Any]) -> str:
    """Stable key for an endpoint plus its parameters.

    Credential parameters are excluded: they do not change the *content* of a
    response in a way worth keying on, and keeping them out means no secret is
    ever part of a filename.
    """
    safe = {
        k: v
        for k, v in sorted(params.items())
        if k.upper() not in {"APIKEY", "PASSWORD", "USERNAME"} and v is not None
    }
    blob = json.dumps([type_name, safe], sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]
    return f"{type_name}-{digest}"


@dataclass(frozen=True, slots=True)
class CacheEntry:
    key: str
    stored_at: float
    expires_at: float
    payload: Any

    @property
    def is_fresh(self) -> bool:
        return time.time() < self.expires_at

    @property
    def age_seconds(self) -> float:
        return time.time() - self.stored_at


class ResponseCache:
    """Filesystem-backed JSON cache."""

    def __init__(self, directory: Path | str = ".cache/mfl", *, clock=time.time) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._clock = clock

    def _path(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    def get(self, key: str, *, allow_stale: bool = False) -> CacheEntry | None:
        path = self._path(key)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # A corrupt cache file is a cache miss, never an error the caller
            # has to handle.
            return None
        entry = CacheEntry(
            key=key,
            stored_at=raw["stored_at"],
            expires_at=raw["expires_at"],
            payload=raw["payload"],
        )
        if allow_stale or self._clock() < entry.expires_at:
            return entry
        return None

    def put(self, key: str, payload: Any, ttl_seconds: int) -> CacheEntry:
        now = self._clock()
        entry = CacheEntry(key, now, now + ttl_seconds, payload)
        self._path(key).write_text(
            json.dumps(
                {
                    "stored_at": entry.stored_at,
                    "expires_at": entry.expires_at,
                    "payload": payload,
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        return entry

    def invalidate(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def clear(self) -> int:
        removed = 0
        for path in self.directory.glob("*.json"):
            path.unlink(missing_ok=True)
            removed += 1
        return removed
