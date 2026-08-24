"""The :class:`NewsSource` interface.

Adding a provider means implementing this and registering it -- no analysis code
changes. The baseline source (MFL's own injury feed) is always on; external
sources are opt-in through ``[news] sources`` in ``config.toml``.

A source returns :class:`~mflbot.domain.models.NewsItem` objects and nothing
else. It does not decide whether news is *actionable*; that judgement belongs to
the analysis engines, which weigh it against projections and league rules.
"""

from __future__ import annotations

import abc
from collections.abc import Iterable, Sequence

from ...domain.models import NewsItem


class Classification:
    """Coarse buckets used to weight a news item's relevance."""

    INJURY = "injury"
    USAGE = "usage"
    DEPTH_CHART = "depth_chart"
    TRANSACTION = "transaction"
    OTHER = "other"


class NewsSource(abc.ABC):
    """A provider of player news."""

    #: Stable identifier used in config and stored on every row.
    source_id: str = "unnamed"
    #: True when the source needs credentials the user has not supplied.
    requires_credentials: bool = False

    @abc.abstractmethod
    def fetch(self, player_ids: Sequence[str] | None = None) -> Iterable[NewsItem]:
        """Return recent items, optionally narrowed to players of interest.

        Implementations must return an empty iterable rather than raising when
        the upstream has nothing to say. They should raise only on a genuine
        failure, so the caller can report a broken source instead of silently
        treating it as quiet.
        """

    def is_available(self) -> tuple[bool, str]:
        """Whether this source can run right now, and why not if it cannot."""
        return True, ""

    def close(self) -> None:  # pragma: no cover - most sources hold nothing
        return None
