"""Interface points for paid news/stats providers.

These are deliberately *not implemented*. Each requires a paid subscription and
an API key that this deployment does not have, and shipping a half-written
client that silently returns nothing would be worse than one that says plainly
that it is unavailable.

This is also why the bot cannot simply get raw stats or news from MFL itself:
MFL's own developer terms state it plainly -- "we can not and will not under
any circumstance make raw NFL player stats available, as that's forbidden per
our stats licensing agreement. Similarly, we can not make third party content
(such as player news) available." MFL's own docs point to exactly this
repo's three stub candidates as the sanctioned way to get raw stats:
FantasyData.com, Sportradar, and XML Team (SportsDataIOSource covers the first
of those). This is not a build limitation to eventually work around by finding
the "real" MFL endpoint for it -- there isn't one.

To add one: subclass :class:`~mflbot.ingest.news.base.NewsSource`, implement
``fetch``, and register it in :mod:`mflbot.ingest.news.registry`. Nothing else
in the bot needs to change.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from ...domain.models import NewsItem
from .base import NewsSource


class _UnimplementedPaidSource(NewsSource):
    requires_credentials = True
    provider_name = "unnamed provider"
    signup_note = ""

    def is_available(self) -> tuple[bool, str]:
        return False, (
            f"{self.provider_name} is a paid provider and this adapter is a stub. "
            f"{self.signup_note}"
        )

    def fetch(self, player_ids: Sequence[str] | None = None) -> Iterable[NewsItem]:
        available, reason = self.is_available()
        raise NotImplementedError(reason)


class SportsDataIOSource(_UnimplementedPaidSource):
    source_id = "sportsdataio"
    provider_name = "SportsDataIO"
    signup_note = (
        "Offers detailed news, projections and snap counts. Requires a paid key; "
        "set MFLBOT_SPORTSDATAIO_KEY and implement fetch() against their NFL news "
        "and projections endpoints."
    )


class FantasyNerdsSource(_UnimplementedPaidSource):
    source_id = "fantasynerds"
    provider_name = "FantasyNerds"
    signup_note = (
        "Cheaper tier than SportsDataIO, covers news and projections. Requires a "
        "paid key; set MFLBOT_FANTASYNERDS_KEY."
    )


class RotowireSource(_UnimplementedPaidSource):
    source_id = "rotowire"
    provider_name = "Rotowire"
    signup_note = (
        "Strong beat-reporter coverage. Licensed feed; set MFLBOT_ROTOWIRE_KEY. "
        "Do not substitute HTML scraping of their site for the licensed feed."
    )
