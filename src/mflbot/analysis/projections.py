"""Projection access.

The bot does not compute its own projections. It reads them from real sources
(MFL's ``projectedScores`` being the built-in one) and reports honestly when a
player has none. A missing projection is never replaced by a positional
average, a last-week carry-forward, or a zero -- each of those would silently
turn "unknown" into "bad", which is a different claim entirely.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from ..errors import Missing
from ..ingest.scores import MFL_PROJECTION_SOURCE


@dataclass(slots=True)
class ProjectionProvider:
    """Reads stored projections for a league/season."""

    repos: object
    league_id: str
    season: int
    source: str = MFL_PROJECTION_SOURCE
    #: One database read per week for the life of this provider. A provider is
    #: built per analysis run, so nothing here outlives the data it came from;
    #: without it, rest-of-season totals re-read a whole week per player.
    _weeks: dict[int, dict[str, float]] = field(default_factory=dict, repr=False)

    def week(self, week: int) -> dict[str, float]:
        if week not in self._weeks:
            self._weeks[week] = self.repos.load_projections(
                self.league_id, self.season, week, self.source
            )
        return self._weeks[week]

    def for_player(self, player_id: str, week: int) -> float | Missing:
        points = self.week(week).get(player_id)
        if points is None:
            return Missing(
                "no projection available for this player and week",
                {"player_id": player_id, "week": week, "source": self.source},
            )
        return points

    def rest_of_season(
        self, player_id: str, weeks: Sequence[int]
    ) -> tuple[float, list[int]] | Missing:
        """Sum projections across ``weeks``.

        Returns the total *and* the weeks that actually contributed, so a
        rationale can say "over 6 of 8 remaining weeks" rather than implying
        full coverage. Blocks entirely when no week has a projection.
        """
        total = 0.0
        covered: list[int] = []
        for week in weeks:
            points = self.week(week).get(player_id)
            if points is None:
                continue
            total += points
            covered.append(week)
        if not covered:
            return Missing(
                "no projections available for this player in any remaining week",
                {"player_id": player_id, "weeks": list(weeks), "source": self.source},
            )
        return round(total, 3), covered

    def coverage(self, player_ids: Sequence[str], week: int) -> float:
        """Fraction of ``player_ids`` that have a projection for ``week``.

        Used to caveat a recommendation whose comparison rests on thin data.
        """
        if not player_ids:
            return 0.0
        available = self.week(week)
        return sum(1 for pid in player_ids if pid in available) / len(player_ids)
