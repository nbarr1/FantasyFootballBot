"""The discord.py adapter: the one module that imports ``discord``.

It does three things, and decides nothing itself:

* Turns each button press, form and slash command into one call on
  :class:`~mflbot.discordbot.actions.DiscordActions`, run on a worker thread
  (the bot's code is blocking: SQLite and MFL requests), and posts the reply.
  Discord needs an answer within 3 seconds, so every interaction is
  acknowledged first; the reply may follow for up to 15 minutes.
* Every ``publish_seconds``, posts a card for each new pending recommendation
  to the owner's DMs, redraws cards whose recommendation has moved on, and
  sends anything waiting in the Discord notifier's outbox.
* Runs on its own thread with its own event loop (:class:`DiscordRunner`), so
  it can sit beside the scheduler and the dashboard in one process.

Buttons are :class:`discord.ui.DynamicItem` instances matched by custom id
(``mflbot:<action>:<recommendation id>``), so a card posted before a restart
still works after it. A card's buttons are only an invitation: every press is
re-checked against the store.
"""

from __future__ import annotations

import asyncio
import logging
import threading

import discord
from discord import app_commands
from discord.ext import tasks

from ..notify.discord import DiscordNotifier, OutgoingMessage
from .actions import NOT_THE_OWNER, ActionResult, DiscordActions
from .render import APPROVE, EDIT, REJECT, SUBMIT, Card, split_message

log = logging.getLogger(__name__)

#: Slash commands the bot offers. Read-only, and none takes more than one
#: recommendation: deciding happens on a card's buttons, one card at a time.
#: ``tests/test_discord.py`` asserts the tree matches this exactly.
COMMANDS = frozenset({"pending", "show", "status", "heartbeat"})

_BUTTON_STYLE = {
    APPROVE: (discord.ButtonStyle.success, "Approve"),
    SUBMIT: (discord.ButtonStyle.primary, "Submit to MFL"),
    REJECT: (discord.ButtonStyle.danger, "Reject"),
    EDIT: (discord.ButtonStyle.secondary, "Edit"),
}

#: Discord caps a modal's title at 45 characters and each input's label too.
_LABEL_LIMIT = 45
#: And a text input's value at 4,000.
_INPUT_LIMIT = 4000

UNEXPECTED_ERROR = "That failed with an unexpected error, logged by the bot."


def custom_id(action: str, recommendation_id: str) -> str:
    return f"mflbot:{action}:{recommendation_id}"


class CardButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"mflbot:(?P<action>approve|submit|reject|edit):(?P<rec>[A-Za-z0-9_-]{1,64})",
):
    """One decision on one recommendation."""

    def __init__(self, action: str, recommendation_id: str) -> None:
        style, label = _BUTTON_STYLE[action]
        super().__init__(
            discord.ui.Button(
                style=style, label=label, custom_id=custom_id(action, recommendation_id)
            )
        )
        self.action = action
        self.recommendation_id = recommendation_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):  # noqa: D102
        return cls(match["action"], match["rec"])

    async def callback(self, interaction: discord.Interaction) -> None:
        client: MFLBotClient = interaction.client  # type: ignore[assignment]
        await client.on_card_button(interaction, self.action, self.recommendation_id)


class RejectModal(discord.ui.Modal):
    note = discord.ui.TextInput(
        label="Why (optional)", required=False, max_length=300,
        style=discord.TextStyle.paragraph,
    )

    def __init__(self, client: MFLBotClient, recommendation_id: str) -> None:
        super().__init__(title="Reject this recommendation", timeout=600)
        self._client = client
        self._recommendation_id = recommendation_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._client.run_action(
            interaction, self._client.actions.reject, self._recommendation_id,
            str(self.note.value or ""),
        )


class EditModal(discord.ui.Modal):
    """One text input per editable payload field (never more than Discord's
    five). Only fields whose value actually changes are edited."""

    def __init__(self, client: MFLBotClient, recommendation_id: str, fields) -> None:
        super().__init__(title="Edit before approving", timeout=900)
        self._client = client
        self._recommendation_id = recommendation_id
        self._inputs: dict[str, discord.ui.TextInput] = {}
        # A value too long for an input is left out rather than cut short: a
        # field the form does not carry is left as it is, and one it carries
        # truncated would be saved truncated.
        fitting = [f for f in fields if len(f["value"]) <= _INPUT_LIMIT]
        for field in fitting[:5]:
            label = field["label"] + (" (comma separated)" if field["is_list"] else "")
            text_input = discord.ui.TextInput(
                label=label[:_LABEL_LIMIT],
                default=field["value"] or None,
                required=False,
                max_length=_INPUT_LIMIT,
                style=(discord.TextStyle.paragraph if field["is_list"]
                       else discord.TextStyle.short),
            )
            self._inputs[field["name"]] = text_input
            self.add_item(text_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        submitted = {name: str(item.value or "") for name, item in self._inputs.items()}
        await self._client.run_action(
            interaction, self._client.actions.edit, self._recommendation_id, submitted,
        )


def build_embed(card: Card) -> discord.Embed:
    embed = discord.Embed(
        title=card.title, description=card.description, colour=card.colour
    )
    for name, value in card.fields:
        embed.add_field(name=name, value=value, inline=False)
    embed.set_footer(text=card.footer)
    return embed


def build_view(card: Card) -> discord.ui.View | None:
    if not card.buttons:
        return None
    view = discord.ui.View(timeout=None)
    for action in card.buttons:
        view.add_item(CardButton(action, card.recommendation_id))
    return view


class MFLBotClient(discord.Client):
    def __init__(
        self,
        actions: DiscordActions,
        *,
        publish_seconds: int = 30,
        notifier: DiscordNotifier | None = None,
    ) -> None:
        # The bot has no voice features, so the missing voice libraries are
        # not worth a warning on every start.
        discord.VoiceClient.warn_nacl = False
        discord.VoiceClient.warn_dave = False
        # No privileged intents: slash commands and buttons arrive as
        # interactions, and the bot never reads message content. Guilds is
        # not privileged, and discord.py's state cache expects it.
        super().__init__(intents=discord.Intents(guilds=True))
        self.actions = actions
        self.notifier = notifier
        self.tree = app_commands.CommandTree(self)
        self._register_commands()
        self._publish_seconds = publish_seconds
        self._dm: discord.DMChannel | None = None
        self._dm_refused = False

    # -- lifecycle ---------------------------------------------------------

    async def setup_hook(self) -> None:
        self.add_dynamic_items(CardButton)
        await self.tree.sync()
        self.publish_loop.change_interval(seconds=self._publish_seconds)
        self.publish_loop.start()

    async def close(self) -> None:
        if self.publish_loop.is_running():
            self.publish_loop.cancel()
        if self.notifier is not None:
            self.notifier.attached = False
        await super().close()

    async def on_ready(self) -> None:
        if self.notifier is not None:
            self.notifier.attached = True
        log.info("Discord bot signed in as %s", self.user)

    # -- slash commands ----------------------------------------------------

    def _register_commands(self) -> None:
        dm_only = app_commands.allowed_contexts(guilds=False, dms=True, private_channels=False)

        @self.tree.command(name="pending", description="What is awaiting your decision")
        @dm_only
        async def pending(interaction: discord.Interaction) -> None:
            await self.run_reply(interaction, self.actions.pending)

        @self.tree.command(name="show", description="Full detail for one recommendation")
        @app_commands.describe(recommendation_id="The id on the card")
        @dm_only
        async def show(interaction: discord.Interaction, recommendation_id: str) -> None:
            await self.run_reply(interaction, self.actions.show, recommendation_id)

        @self.tree.command(name="status", description="What is stored, blocked and running")
        @dm_only
        async def status(interaction: discord.Interaction) -> None:
            await self.run_reply(interaction, self.actions.status)

        @self.tree.command(name="heartbeat", description="Whether the scheduled jobs keep up")
        @dm_only
        async def heartbeat(interaction: discord.Interaction) -> None:
            await self.run_reply(interaction, self.actions.heartbeat)

    async def run_reply(self, interaction: discord.Interaction, call, *args) -> None:
        """Answer a read-only command with its text, split to fit."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await self._call(call, interaction.user.id, *args)
        for chunk in split_message(result.message):
            await interaction.followup.send(chunk, ephemeral=True)

    async def _call(self, call, user_id, *args) -> ActionResult:
        """Run one action on a worker thread. An unexpected error still gets a
        reply, or Discord would show the bot "thinking" until the interaction
        expires."""
        try:
            return await asyncio.to_thread(call, user_id, *args)
        except Exception:  # noqa: BLE001 - reported to the user and logged
            log.exception("Discord action %s failed", getattr(call, "__name__", call))
            if args and isinstance(args[0], str):
                return ActionResult(
                    False,
                    f"{UNEXPECTED_ERROR} Check where this recommendation stands with "
                    f"`/show {args[0]}` before trying again.",
                    args[0],
                )
            return ActionResult(False, UNEXPECTED_ERROR)

    # -- buttons and forms -------------------------------------------------

    async def on_card_button(
        self, interaction: discord.Interaction, action: str, recommendation_id: str
    ) -> None:
        if not self.actions.is_owner(interaction.user.id):
            await interaction.response.send_message(NOT_THE_OWNER, ephemeral=True)
            return
        if action == REJECT:
            # A form must be the first response, so it cannot wait on a thread.
            await interaction.response.send_modal(RejectModal(self, recommendation_id))
            return
        if action == EDIT:
            fields = await asyncio.to_thread(
                self.actions.edit_form, interaction.user.id, recommendation_id
            )
            if isinstance(fields, ActionResult):
                await interaction.response.send_message(fields.message, ephemeral=True)
                return
            await interaction.response.send_modal(
                EditModal(self, recommendation_id, fields)
            )
            return
        call = self.actions.approve if action == APPROVE else self.actions.submit
        await self.run_action(interaction, call, recommendation_id)

    async def run_action(self, interaction: discord.Interaction, call, *args) -> None:
        """Acknowledge, decide on a worker thread, reply, and redraw the card."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await self._call(call, interaction.user.id, *args)
        await interaction.followup.send(result.message[:2000], ephemeral=True)
        if result.recommendation_id:
            await self.redraw(result.recommendation_id, interaction.message)

    # -- cards -------------------------------------------------------------

    async def owner_dm(self) -> discord.DMChannel:
        if self._dm is None:
            user = await self.fetch_user(int(self.actions.owner_user_id))
            self._dm = user.dm_channel or await user.create_dm()
        return self._dm

    async def post(self, recommendation_id: str) -> None:
        card = await asyncio.to_thread(self.actions.card, recommendation_id)
        if card is None:
            return
        dm = await self.owner_dm()
        kwargs = {"embed": build_embed(card)}
        view = build_view(card)
        if view is not None:
            kwargs["view"] = view
        message = await dm.send(**kwargs)
        await asyncio.to_thread(
            self.actions.record_card, recommendation_id, dm.id, message.id, card.key
        )

    async def redraw(self, recommendation_id: str, message=None) -> None:
        """Redraw a posted card to match its recommendation now."""
        card = await asyncio.to_thread(self.actions.card, recommendation_id)
        if card is None:
            return
        if message is None:
            record = await asyncio.to_thread(self.actions.card_record, recommendation_id)
            if record is None:
                return
            dm = await self.owner_dm()
            try:
                message = await dm.fetch_message(int(record.message_id))
            except discord.NotFound:
                await asyncio.to_thread(self.actions.forget_card, recommendation_id)
                return
        await message.edit(embed=build_embed(card), view=build_view(card))
        await asyncio.to_thread(
            self.actions.record_card, recommendation_id, message.channel.id,
            message.id, card.key,
        )

    @tasks.loop(seconds=30)
    async def publish_loop(self) -> None:
        # A task loop stops for good on an unhandled exception, so nothing may
        # escape: one failed pass is logged and the next one tries again.
        try:
            plan = await asyncio.to_thread(self.actions.publication_plan)
            for recommendation in plan.to_post:
                await self.post(recommendation.id)
            for recommendation, _record in plan.to_refresh:
                await self.redraw(recommendation.id)
            if self.notifier is not None:
                for message in self.notifier.drain():
                    await self.send_notification(message)
            self._dm_refused = False
        except discord.Forbidden:
            # Said once, not every pass: the fix is on the owner's side.
            if not self._dm_refused:
                log.error(
                    "Discord refused to deliver a DM to the owner. The bot must "
                    "share a server with you, and your privacy settings must allow "
                    "DMs from that server's members. Retrying every pass."
                )
            self._dm_refused = True
        except Exception:  # noqa: BLE001 - reported, and retried next pass
            log.exception("Discord publishing pass failed")

    @publish_loop.before_loop
    async def _wait_until_ready(self) -> None:
        await self.wait_until_ready()

    async def send_notification(self, message: OutgoingMessage) -> None:
        dm = await self.owner_dm()
        prefix = "🚨 " if message.urgent else ""
        text = f"{prefix}**{message.subject}**\n{message.body}"
        for chunk in split_message(text):
            await dm.send(chunk)


class DiscordRunner:
    """Runs the bot on its own thread and event loop, beside the rest.

    ``start`` returns at once; ``stop`` closes the connection and waits for
    the thread. The token is handed to discord.py and nowhere else.
    """

    def __init__(self, client: MFLBotClient, token: str) -> None:
        self._client = client
        self._token = token
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="mflbot-discord", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            asyncio.run(self._main())
        except discord.LoginFailure:
            log.error(
                "The Discord bot stopped: Discord refused the token in "
                "MFLBOT_DISCORD_TOKEN. Reset it in the Developer Portal, update "
                "the secrets file, and restart."
            )
        except Exception:  # noqa: BLE001 - a dead bot is logged, not a dead process
            log.exception("The Discord bot stopped")

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        async with self._client:
            await self._client.start(self._token)

    def stop(self, timeout: float = 10.0) -> None:
        if self._loop is not None and not self._client.is_closed():
            future = asyncio.run_coroutine_threadsafe(self._client.close(), self._loop)
            try:
                future.result(timeout=timeout)
            except Exception:  # noqa: BLE001 - shutting down regardless
                log.warning("The Discord bot did not close cleanly")
        if self._thread is not None:
            self._thread.join(timeout=timeout)
