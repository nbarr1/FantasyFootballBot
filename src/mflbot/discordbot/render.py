"""A recommendation as a Discord card, independent of the Discord library.

The card carries what the dashboard's detail page carries -- the action, the
reasoning, the caveats, the literal payload and the expiry -- built from the
same view model (:mod:`mflbot.web.views`), so the two surfaces show the same
facts. Discord caps every part of a message; anything cut to fit says where the
full text is (``/show <id>``) rather than silently losing it.

Which buttons a card carries follows the recommendation's state, and a button
is only ever an invitation: whatever it asks for is re-checked against the
store when it is pressed.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..recommend.models import Recommendation, RecommendationStatus
from ..web import views

#: Discord's limits, as documented for embeds and messages.
TITLE_LIMIT = 256
DESCRIPTION_LIMIT = 4096
FIELD_VALUE_LIMIT = 1024
FOOTER_LIMIT = 2048
MESSAGE_LIMIT = 2000

#: Kept well inside the 6,000-character total an embed may hold.
DESCRIPTION_BUDGET = 1200

#: Button actions, in the order they appear on a card.
APPROVE, SUBMIT, REJECT, EDIT = "approve", "submit", "reject", "edit"

_COLOURS = {
    "pending": 0x3B82F6,
    "approved": 0x8B5CF6,
    "good": 0x16A34A,
    "bad": 0xDC2626,
    "muted": 0x6B7280,
}


@dataclass(frozen=True, slots=True)
class Card:
    recommendation_id: str
    title: str
    description: str
    fields: tuple[tuple[str, str], ...]
    footer: str
    colour: int
    buttons: tuple[str, ...]
    #: Identifies what the card shows, so the publisher can tell when a posted
    #: card is out of date: the status, plus whether an approval is live.
    key: str


def clip(text: str, limit: int) -> tuple[str, bool]:
    """``text`` cut to ``limit`` characters, and whether it was cut."""
    if len(text) <= limit:
        return text, False
    return text[: limit - 1].rstrip() + "…", True


def card_key(recommendation: Recommendation, live_approval: bool) -> str:
    live = "+live" if live_approval else ""
    return f"{recommendation.status}{live}"


def buttons_for(
    recommendation: Recommendation, *, live_approval: bool, allow_submissions: bool
) -> tuple[str, ...]:
    """The decisions still open on this recommendation, as buttons."""
    if recommendation.is_expired:
        return ()
    if recommendation.status == RecommendationStatus.PROPOSED:
        return (APPROVE, REJECT, EDIT)
    if recommendation.status == RecommendationStatus.APPROVED:
        if not live_approval:
            # The approval expired before it was submitted: approve again.
            return (APPROVE, REJECT, EDIT)
        return ((SUBMIT,) if allow_submissions else ()) + (REJECT, EDIT)
    return ()  # executed, failed, rejected, expired: a record, not a decision


def build_card(
    recommendation: Recommendation,
    names: dict[str, str],
    *,
    live_approval: bool,
    allow_submissions: bool,
) -> Card:
    view = views.recommendation_view(recommendation, names)
    cut = False

    title, was_cut = clip(
        f"{view['kind_label']} · {view['confidence']} confidence · {view['status']}",
        TITLE_LIMIT,
    )
    cut |= was_cut
    description, was_cut = clip(
        f"**{view['summary']}**\nExpires {view['expires_at']} ({view['expires_in']})",
        DESCRIPTION_BUDGET,
    )
    cut |= was_cut

    fields: list[tuple[str, str]] = []
    why, was_cut = clip(view["rationale"] or "(none given)", FIELD_VALUE_LIMIT)
    cut |= was_cut
    fields.append(("Why", why))
    if view["caveats"]:
        caveats, was_cut = clip(
            "\n".join(f"- {c}" for c in view["caveats"]), FIELD_VALUE_LIMIT
        )
        cut |= was_cut
        fields.append(("Caveats", caveats))
    payload_lines = []
    for row in view["payload_rows"]:
        named = f"  ({', '.join(row['names'])})" if row["names"] else ""
        payload_lines.append(f"{row['key']} = {row['value']}{named}")
    # Room for the code fence around it.
    payload, was_cut = clip("\n".join(payload_lines), FIELD_VALUE_LIMIT - 8)
    cut |= was_cut
    fields.append(("Exact payload", f"```\n{payload}\n```"))

    footer = f"Recommendation {recommendation.id}"
    if cut:
        footer += f" · shortened to fit; full detail: /show {recommendation.id}"
    footer, _ = clip(footer, FOOTER_LIMIT)

    return Card(
        recommendation_id=recommendation.id,
        title=title,
        description=description,
        fields=tuple(fields),
        footer=footer,
        colour=_COLOURS.get(view["tone"], _COLOURS["muted"]),
        buttons=buttons_for(
            recommendation, live_approval=live_approval,
            allow_submissions=allow_submissions,
        ),
        key=card_key(recommendation, live_approval),
    )


def split_message(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Split ``text`` into messages under Discord's limit, at line breaks
    where possible, losing nothing."""
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:  # a single overlong line: hard-split it
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = ""
        current += line
    if current:
        chunks.append(current)
    return chunks or [""]
