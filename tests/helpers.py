"""Shared test helpers.

Kept out of ``conftest.py`` on purpose: conftest is loaded by pytest as a
plugin, not as an importable module, so ``from tests.conftest import ...`` only
works when the repository root happens to be on ``sys.path`` -- true for
``python -m pytest``, false for the bare ``pytest`` console script that CI runs.
A plain module plus ``pythonpath`` in pyproject works under either invocation.

Everything here builds SYNTHETIC data for exercising logic. None of it is a
default, a seed, or a fallback.
"""

from __future__ import annotations

from mflbot.errors import Missing


class FakeProjections:
    """Stands in for ProjectionProvider with SYNTHETIC per-week points."""

    def __init__(self, by_week: dict[int, dict[str, float]]) -> None:
        self._by_week = by_week

    def week(self, week: int) -> dict[str, float]:
        return self._by_week.get(week, {})

    def rest_of_season(self, player_id: str, weeks):
        total, covered = 0.0, []
        for week in weeks:
            points = self.week(week).get(player_id)
            if points is None:
                continue
            total += points
            covered.append(week)
        if not covered:
            return Missing("no projections", {"player_id": player_id})
        return round(total, 3), covered

    def coverage(self, player_ids, week: int) -> float:
        if not player_ids:
            return 0.0
        available = self.week(week)
        return sum(1 for pid in player_ids if pid in available) / len(player_ids)
