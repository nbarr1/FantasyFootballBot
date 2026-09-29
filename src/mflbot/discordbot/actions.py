"""What each Discord button and command does -- with no Discord code in it.

The adapter in :mod:`mflbot.discordbot.client` turns a click or a slash command
into one call here, with the Discord user id that made it, and posts whatever
comes back. Everything that decides lives on this side of that line, so it is
tested without Discord.

The rules are the other surfaces' rules, not a second copy of them:

* **Only the owner decides.** Every call checks the user id against
  ``[discord] owner_user_id`` before touching anything. Anyone else is refused
  and nothing changes. The read-only commands are the owner's too: they show
  league data.
* **One recommendation per action.** Every deciding call takes exactly one
  recommendation id. There is no batch.
* **Decisions go through the shared code.** Approve, reject and edit use
  :class:`~mflbot.approval.discord_channel.DiscordApprovalChannel`, which
  delegates to the CLI channel; submitting uses
  :meth:`~mflbot.context.BotContext.submit_approved`, the dashboard's path.
  The executor re-checks the stored state before anything is sent, so a
  button on an out-of-date card cannot act on a rejected or edited
  recommendation.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..approval.discord_channel import DiscordApprovalChannel, discord_actor
from ..errors import MFLBotError
from ..web import views
from . import publisher
from .render import Card, build_card

NOT_THE_OWNER = "Only this bot's owner can use it. Nothing was changed."
SUBMISSIONS_OFF = (
    "Submitting from Discord is turned off ([discord] allow_submissions). The "
    "approval is recorded; submit it from the dashboard or with `bot execute`."
)


@dataclass(frozen=True, slots=True)
class ActionResult:
    ok: bool
    message: str
    #: The recommendation whose card should be redrawn, if any.
    recommendation_id: str | None = None


class DiscordActions:
    def __init__(self, context, settings) -> None:
        self._context = context
        self._owner = settings.owner_user_id.strip()
        self._allow_submissions = settings.allow_submissions
        self._channel = DiscordApprovalChannel(
            context.store, context.tokens, context.notifier
        )

    @property
    def owner_user_id(self) -> str:
        return self._owner

    @property
    def allow_submissions(self) -> bool:
        return self._allow_submissions

    def is_owner(self, user_id) -> bool:
        return bool(self._owner) and str(user_id) == self._owner

    # -- decisions ---------------------------------------------------------

    def approve(self, user_id, recommendation_id: str) -> ActionResult:
        if not self.is_owner(user_id):
            return ActionResult(False, NOT_THE_OWNER)
        try:
            self._channel.approve(recommendation_id, actor=discord_actor(user_id))
        except MFLBotError as exc:
            return ActionResult(False, str(exc), recommendation_id)
        if self._allow_submissions:
            note = "Nothing has been sent to MFL yet -- press Submit when ready."
        else:
            note = SUBMISSIONS_OFF
        return ActionResult(
            True,
            f"Approved {recommendation_id}. The approval is bound to this exact "
            f"payload and is valid once. {note}",
            recommendation_id,
        )

    def submit(self, user_id, recommendation_id: str) -> ActionResult:
        if not self.is_owner(user_id):
            return ActionResult(False, NOT_THE_OWNER)
        if not self._allow_submissions:
            return ActionResult(False, SUBMISSIONS_OFF, recommendation_id)
        try:
            outcome = self._context.submit_approved(recommendation_id)
        except MFLBotError as exc:
            return ActionResult(False, f"Not submitted: {exc}", recommendation_id)
        return ActionResult(outcome.ok, outcome.message, recommendation_id)

    def reject(self, user_id, recommendation_id: str, note: str = "") -> ActionResult:
        if not self.is_owner(user_id):
            return ActionResult(False, NOT_THE_OWNER)
        try:
            self._channel.reject(
                recommendation_id, actor=discord_actor(user_id), note=note
            )
        except MFLBotError as exc:
            return ActionResult(False, str(exc), recommendation_id)
        return ActionResult(
            True, f"Rejected {recommendation_id}. Nothing was submitted.",
            recommendation_id,
        )

    def edit_form(self, user_id, recommendation_id: str) -> list[dict] | ActionResult:
        """The fields to put in the Edit form, or why there is no form."""
        if not self.is_owner(user_id):
            return ActionResult(False, NOT_THE_OWNER)
        recommendation = self._context.store.get(recommendation_id)
        if recommendation is None:
            return ActionResult(False, f"No recommendation with id {recommendation_id!r}.")
        fields = views.editable_fields(recommendation)
        if not fields:
            return ActionResult(False, "This recommendation has nothing editable.")
        return fields

    def edit(self, user_id, recommendation_id: str, submitted: dict[str, str]) -> ActionResult:
        if not self.is_owner(user_id):
            return ActionResult(False, NOT_THE_OWNER)
        recommendation = self._context.store.get(recommendation_id)
        if recommendation is None:
            return ActionResult(False, f"No recommendation with id {recommendation_id!r}.")
        try:
            changes = views.changed_fields(recommendation, submitted)
        except ValueError as exc:
            return ActionResult(False, str(exc), recommendation_id)
        if not changes:
            return ActionResult(True, "Nothing changed.", recommendation_id)
        try:
            decision = self._channel.edit(
                recommendation_id, changes, actor=discord_actor(user_id)
            )
        except (MFLBotError, ValueError) as exc:
            return ActionResult(False, f"Edit refused: {exc}", recommendation_id)
        return ActionResult(
            True, f"Edited {', '.join(sorted(changes))}. {decision.note}",
            recommendation_id,
        )

    # -- reading -----------------------------------------------------------

    def pending(self, user_id) -> ActionResult:
        if not self.is_owner(user_id):
            return ActionResult(False, NOT_THE_OWNER)
        self._context.store.expire_stale()
        pending = self._context.store.pending()
        if not pending:
            return ActionResult(True, "Nothing is awaiting your decision.")
        lines = [f"{len(pending)} recommendation(s) awaiting your decision:"]
        lines.extend(
            f"- `{r.id}` {r.payload.describe()} (expires {r.expires_at:%a %H:%M UTC})"
            for r in pending
        )
        lines.append("Each has a card with its buttons; `/show <id>` for full detail.")
        return ActionResult(True, "\n".join(lines))

    def show(self, user_id, recommendation_id: str) -> ActionResult:
        if not self.is_owner(user_id):
            return ActionResult(False, NOT_THE_OWNER)
        recommendation = self._context.store.get(recommendation_id)
        if recommendation is None:
            return ActionResult(False, f"No recommendation with id {recommendation_id!r}.")
        return ActionResult(
            True,
            f"{recommendation.render()}\n\nStatus: {recommendation.status}",
            recommendation_id,
        )

    def status(self, user_id) -> ActionResult:
        if not self.is_owner(user_id):
            return ActionResult(False, NOT_THE_OWNER)
        status = views.status_view(self._context)
        lines = [
            f"League {status['league_id']} ({status['season']})"
            + (f" -- {status['league_name']}" if status["league_name"] else ""),
            f"Awaiting your decision: {status['pending_count']}",
            f"Write capabilities verified: {status['writes_verified']}/"
            f"{status['writes_total']}",
            f"Scheduler jobs: {status['watchdog']['summary']}",
            f"Submitting from Discord: {'on' if self._allow_submissions else 'off'}",
        ]
        for blocked in status["blocked_features"]:
            lines.append(f"Blocked -- {blocked['feature']}: {blocked['reason']}")
        return ActionResult(True, "\n".join(lines))

    def heartbeat(self, user_id) -> ActionResult:
        if not self.is_owner(user_id):
            return ActionResult(False, NOT_THE_OWNER)
        from ..schedule.heartbeat import check

        return ActionResult(True, check(self._context.repos, self._context.config).render())

    # -- cards -------------------------------------------------------------

    def card(self, recommendation_id: str) -> Card | None:
        """The card for a recommendation as it stands now."""
        recommendation = self._context.store.get(recommendation_id)
        if recommendation is None:
            return None
        names = views.player_names(
            self._context, views.recommendation_ids([recommendation])
        )
        return build_card(
            recommendation,
            names,
            live_approval=self._context.tokens.latest_for(recommendation_id) is not None,
            allow_submissions=self._allow_submissions,
        )

    def publication_plan(self) -> publisher.PublishPlan:
        return publisher.plan(self._context.store, self._context.repos, self._context.tokens)

    def card_record(self, recommendation_id: str) -> publisher.CardRecord | None:
        return publisher.load_card(self._context.repos, recommendation_id)

    def record_card(self, recommendation_id: str, channel_id, message_id, key: str) -> None:
        publisher.record_card(
            self._context.repos,
            recommendation_id,
            publisher.CardRecord(str(channel_id), str(message_id), key),
        )

    def forget_card(self, recommendation_id: str) -> None:
        """Drop the record of a card that no longer exists on Discord."""
        self._context.repos.set_state(publisher.CARD_STATE_PREFIX + recommendation_id, "")
