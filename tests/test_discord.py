"""The Discord bot: the same invariants as the other surfaces, through buttons.

Almost all of this runs without Discord: the deciding code
(:mod:`mflbot.discordbot.actions`), the cards and the publisher have no
discord.py in them. The last few tests check the adapter's shape when
discord.py is installed.

The context is the dashboard tests' synthetic league, with one pending lineup
recommendation. Every id here is SYNTHETIC.
"""

from __future__ import annotations

import dataclasses
import os
import stat

import pytest
from test_web_app import context  # noqa: F401 - fixture, reused rather than rebuilt

from mflbot.config import DiscordSettings, load_config
from mflbot.context import BotContext
from mflbot.discordbot import publisher
from mflbot.discordbot.actions import NOT_THE_OWNER, SUBMISSIONS_OFF, DiscordActions
from mflbot.discordbot.render import (
    APPROVE,
    EDIT,
    FIELD_VALUE_LIMIT,
    REJECT,
    SUBMIT,
    build_card,
    split_message,
)
from mflbot.errors import ConfigError
from mflbot.notify.discord import DiscordNotifier
from mflbot.recommend.models import RecommendationStatus

OWNER = "100000000000000001"
STRANGER = "200000000000000002"


def actions_for(context, **settings) -> DiscordActions:  # noqa: F811
    return DiscordActions(
        context, DiscordSettings(enabled=True, owner_user_id=OWNER, **settings)
    )


def the_recommendation(context):  # noqa: F811
    [recommendation] = context.store.pending()
    return recommendation


@pytest.fixture
def executed(monkeypatch):
    """Record what would have been sent to MFL, without sending anything."""
    calls = []

    class Outcome:
        ok = True
        message = "Submitted and confirmed (synthetic)."

    def fake_execute(self, recommendation, token):
        calls.append((recommendation.id, token.token_id))
        return Outcome()

    monkeypatch.setattr(BotContext, "execute_approved", fake_execute)
    return calls


# ---------------------------------------------------------------------------
# only the owner decides
# ---------------------------------------------------------------------------

def test_nobody_but_the_owner_can_do_anything(context, executed) -> None:  # noqa: F811
    actions = actions_for(context)
    recommendation = the_recommendation(context)
    rid = recommendation.id

    results = [
        actions.approve(STRANGER, rid),
        actions.submit(STRANGER, rid),
        actions.reject(STRANGER, rid, "no"),
        actions.edit(STRANGER, rid, {"week": "9"}),
        actions.edit_form(STRANGER, rid),
        actions.pending(STRANGER),
        actions.show(STRANGER, rid),
        actions.status(STRANGER),
        actions.heartbeat(STRANGER),
    ]

    assert all(r.ok is False and r.message == NOT_THE_OWNER for r in results)
    stored = context.store.get(rid)
    assert stored.status == RecommendationStatus.PROPOSED
    assert stored.payload == recommendation.payload
    assert context.tokens.latest_for(rid) is None
    assert executed == []


def test_no_owner_means_nobody(context) -> None:  # noqa: F811
    actions = DiscordActions(context, DiscordSettings(enabled=False, owner_user_id=""))
    assert not actions.is_owner("")
    assert not actions.approve("", the_recommendation(context).id).ok


# ---------------------------------------------------------------------------
# deciding
# ---------------------------------------------------------------------------

def test_approving_records_who_and_submits_nothing(context, executed) -> None:  # noqa: F811
    actions = actions_for(context)
    rid = the_recommendation(context).id

    result = actions.approve(OWNER, rid)

    assert result.ok and result.recommendation_id == rid
    assert context.store.get(rid).status == RecommendationStatus.APPROVED
    token = context.tokens.latest_for(rid)
    assert token is not None and token.approved_by == f"discord:{OWNER}"
    assert executed == []


def test_submit_spends_the_one_approval(context, executed) -> None:  # noqa: F811
    actions = actions_for(context)
    rid = the_recommendation(context).id
    actions.approve(OWNER, rid)
    token = context.tokens.latest_for(rid)

    result = actions.submit(OWNER, rid)

    assert result.ok
    assert executed == [(rid, token.token_id)]


def test_submitting_without_an_approval_sends_nothing(context, executed) -> None:  # noqa: F811
    result = actions_for(context).submit(OWNER, the_recommendation(context).id)
    assert not result.ok and "no live approval" in result.message
    assert executed == []


def test_an_out_of_date_card_cannot_submit_a_rejected_action(context, executed) -> None:  # noqa: F811
    actions = actions_for(context)
    rid = the_recommendation(context).id
    actions.approve(OWNER, rid)
    actions.reject(OWNER, rid, "changed my mind")

    # The card's Submit button is still on screen until it is redrawn.
    result = actions.submit(OWNER, rid)

    assert not result.ok
    assert executed == []
    assert context.store.get(rid).status == RecommendationStatus.REJECTED


def test_editing_withdraws_the_approval(context) -> None:  # noqa: F811
    actions = actions_for(context)
    recommendation = the_recommendation(context)
    actions.approve(OWNER, recommendation.id)

    result = actions.edit(OWNER, recommendation.id, {"week": str(recommendation.payload.week + 1)})

    assert result.ok and "week" in result.message
    stored = context.store.get(recommendation.id)
    assert stored.status == RecommendationStatus.PROPOSED
    assert stored.payload.week == recommendation.payload.week + 1
    assert context.tokens.latest_for(recommendation.id) is None


def test_an_edit_form_that_changes_nothing_changes_nothing(context) -> None:  # noqa: F811
    actions = actions_for(context)
    recommendation = the_recommendation(context)
    fields = actions.edit_form(OWNER, recommendation.id)
    unchanged = {f["name"]: f["value"] for f in fields}

    assert actions.edit(OWNER, recommendation.id, unchanged).message == "Nothing changed."
    assert context.store.get(recommendation.id).payload == recommendation.payload


def test_an_action_cannot_be_emptied_by_an_edit(context) -> None:  # noqa: F811
    actions = actions_for(context)
    rid = the_recommendation(context).id
    result = actions.edit(OWNER, rid, {"starter_ids": ""})
    assert not result.ok and "cannot be emptied" in result.message


def test_the_edit_form_fits_in_one_discord_modal(context) -> None:  # noqa: F811
    fields = actions_for(context).edit_form(OWNER, the_recommendation(context).id)
    assert 1 <= len(fields) <= 5


def test_with_submitting_off_nothing_is_ever_sent(context, executed) -> None:  # noqa: F811
    actions = actions_for(context, allow_submissions=False)
    rid = the_recommendation(context).id
    approved = actions.approve(OWNER, rid)

    submitted = actions.submit(OWNER, rid)

    assert approved.ok and SUBMISSIONS_OFF in approved.message
    assert not submitted.ok and submitted.message == SUBMISSIONS_OFF
    assert executed == []
    assert SUBMIT not in actions.card(rid).buttons


# ---------------------------------------------------------------------------
# cards
# ---------------------------------------------------------------------------

def test_a_card_offers_only_the_decisions_still_open(context) -> None:  # noqa: F811
    actions = actions_for(context)
    rid = the_recommendation(context).id

    assert actions.card(rid).buttons == (APPROVE, REJECT, EDIT)
    actions.approve(OWNER, rid)
    assert actions.card(rid).buttons == (SUBMIT, REJECT, EDIT)
    actions.reject(OWNER, rid)
    assert actions.card(rid).buttons == ()


def test_a_long_card_fits_discords_limits_and_says_where_the_rest_is(context) -> None:  # noqa: F811
    recommendation = dataclasses.replace(
        the_recommendation(context),
        rationale="A very long reason. " * 800,
        caveats=tuple(f"caveat number {i} " * 20 for i in range(40)),
    )
    card = build_card(recommendation, {}, live_approval=False, allow_submissions=True)

    assert len(card.title) <= 256
    assert len(card.description) <= 4096
    assert all(len(value) <= FIELD_VALUE_LIMIT for _, value in card.fields)
    total = len(card.title) + len(card.description) + len(card.footer) + sum(
        len(name) + len(value) for name, value in card.fields
    )
    assert total <= 6000
    assert f"/show {recommendation.id}" in card.footer


def test_the_card_shows_the_literal_payload(context) -> None:  # noqa: F811
    card = actions_for(context).card(the_recommendation(context).id)
    payload = dict(card.fields)["Exact payload"]
    assert "starter_ids" in payload and "league_id" in payload


def test_long_text_is_split_without_losing_any() -> None:
    text = "\n".join(f"line {i} " + "x" * 90 for i in range(200)) + "\n" + "y" * 4500
    chunks = split_message(text)
    assert all(len(chunk) <= 2000 for chunk in chunks)
    assert "".join(chunks) == text


# ---------------------------------------------------------------------------
# publishing
# ---------------------------------------------------------------------------

def test_each_pending_recommendation_is_posted_once(context) -> None:  # noqa: F811
    actions = actions_for(context)
    rid = the_recommendation(context).id

    first = actions.publication_plan()
    assert [r.id for r in first.to_post] == [rid]
    actions.record_card(rid, 11, 22, actions.card(rid).key)

    # A restart builds everything afresh from the same database.
    again = actions_for(context).publication_plan()
    assert again.to_post == [] and again.to_refresh == []


def test_a_card_is_redrawn_when_its_recommendation_moves_on(context) -> None:  # noqa: F811
    actions = actions_for(context)
    rid = the_recommendation(context).id
    actions.record_card(rid, 11, 22, actions.card(rid).key)

    actions.approve(OWNER, rid)  # e.g. approved from the dashboard instead

    [(recommendation, record)] = actions.publication_plan().to_refresh
    assert recommendation.id == rid and record.message_id == "22"


def test_a_card_record_survives_a_round_trip() -> None:
    record = publisher.CardRecord("11", "22", "approved+live")
    assert publisher.CardRecord.decode(record.encode()) == record
    assert publisher.CardRecord.decode("") is None
    assert publisher.CardRecord.decode("not/a/record") is None


# ---------------------------------------------------------------------------
# notifications
# ---------------------------------------------------------------------------

def test_the_notifier_logs_when_no_bot_is_running() -> None:
    assert DiscordNotifier().send("subject", "body") is False


def test_a_full_outbox_drops_the_oldest_and_never_blocks() -> None:
    notifier = DiscordNotifier(maxsize=3)
    notifier.attached = True
    for i in range(5):
        assert notifier.send(f"message {i}", "") is True
    assert [m.subject for m in notifier.drain()] == ["message 2", "message 3", "message 4"]
    assert notifier.dropped == 2


def test_recommendations_are_not_repeated_as_notifications(context) -> None:  # noqa: F811
    notifier = DiscordNotifier()
    notifier.attached = True
    context.notifier = notifier
    recommendation = the_recommendation(context)

    context._announce("1 add/drop idea(s)", [recommendation])
    assert notifier.drain() == [], "the card is the notification"

    context._announce("Week 5 lineup", [recommendation], urgent=True)
    [message] = notifier.drain()
    assert message.urgent and "Week 5 lineup" in message.subject


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------

def _config(tmp_path, discord_table: str, top: str = ""):
    path = tmp_path / "config.toml"
    path.write_text(
        top + '[league]\nid = "TEST0001"\nseason = 2026\nhost = "example.invalid"\n'
        + discord_table
    )
    return path


def test_discord_cannot_be_enabled_without_an_owner(tmp_path) -> None:
    with pytest.raises(ConfigError, match="owner_user_id"):
        load_config(_config(tmp_path, "[discord]\nenabled = true\n"))


def test_an_owner_must_be_a_discord_id(tmp_path) -> None:
    with pytest.raises(ConfigError, match="digits"):
        load_config(_config(tmp_path, '[discord]\nowner_user_id = "me"\n'))


def test_a_numeric_owner_id_is_read_as_text(tmp_path) -> None:
    config = load_config(
        _config(tmp_path, f"[discord]\nenabled = true\nowner_user_id = {OWNER}\n",
                top='approval_channel = "discord"\n')
    )
    assert config.discord.owner_user_id == OWNER
    assert config.approval_channel == "discord"


@pytest.mark.parametrize(
    ("top", "table", "match"),
    [
        ("", '[notify]\ntransport = "discord"\n', "transport"),
        ('approval_channel = "discord"\n', "", "approval_channel"),
        ("", f"[discord]\nowner_user_id = {OWNER}\npublish_seconds = 0\n", "publish_seconds"),
    ],
)
def test_settings_that_point_at_a_bot_that_never_runs_are_refused(
    tmp_path, top, table, match
) -> None:
    with pytest.raises(ConfigError, match=match):
        load_config(_config(tmp_path, table, top=top))


def test_the_token_is_read_from_the_environment_or_the_secrets_file(tmp_path) -> None:
    from mflbot.mfl.auth import read_secret

    secrets = tmp_path / "secrets.env"
    secrets.write_text("MFLBOT_DISCORD_TOKEN=from-file\n")
    os.chmod(secrets, stat.S_IRUSR | stat.S_IWUSR)

    assert read_secret("MFLBOT_DISCORD_TOKEN", {"MFLBOT_DISCORD_TOKEN": "from-env"}) == "from-env"
    assert read_secret(
        "MFLBOT_DISCORD_TOKEN", {"MFLBOT_SECRETS_FILE": str(secrets)}
    ) == "from-file"
    assert read_secret("MFLBOT_DISCORD_TOKEN", {}) is None


def test_the_bot_only_starts_when_enabled_and_refuses_without_a_token(
    context, monkeypatch  # noqa: F811
) -> None:
    from mflbot.cli import start_discord

    assert start_discord(context) is None  # disabled by default

    context.config = dataclasses.replace(
        context.config, discord=DiscordSettings(enabled=True, owner_user_id=OWNER)
    )
    monkeypatch.delenv("MFLBOT_DISCORD_TOKEN", raising=False)
    monkeypatch.delenv("MFLBOT_SECRETS_FILE", raising=False)
    with pytest.raises(ConfigError, match="MFLBOT_DISCORD_TOKEN"):
        start_discord(context)


# ---------------------------------------------------------------------------
# the discord.py adapter's shape
# ---------------------------------------------------------------------------

def test_the_slash_commands_are_read_only_and_dm_only(context) -> None:  # noqa: F811
    pytest.importorskip("discord")
    from mflbot.discordbot.client import COMMANDS, MFLBotClient

    client = MFLBotClient(actions_for(context))
    commands = {c.name: c for c in client.tree.get_commands()}

    assert set(commands) == COMMANDS
    for command in commands.values():
        contexts = command.allowed_contexts
        assert contexts.dm_channel and not contexts.guild and not contexts.private_channel
        # No command takes a list of recommendations: at most one id.
        assert [p.name for p in command.parameters] in ([], ["recommendation_id"])


def test_the_bot_asks_for_no_privileged_intents(context) -> None:  # noqa: F811
    pytest.importorskip("discord")
    from mflbot.discordbot.client import MFLBotClient

    intents = MFLBotClient(actions_for(context)).intents
    assert not (intents.message_content or intents.members or intents.presences)


def test_a_button_names_exactly_one_known_action_and_one_recommendation() -> None:
    pytest.importorskip("discord")
    from mflbot.discordbot.client import CardButton

    template = CardButton.__discord_ui_compiled_template__
    assert template.fullmatch("mflbot:approve:abc123")
    assert template.fullmatch("mflbot:submit:abc123")
    assert not template.fullmatch("mflbot:approve_all:abc123")
    assert not template.fullmatch("mflbot:approve:abc123,def456")
    assert not template.fullmatch("mflbot:approve:")


def test_a_decided_card_has_no_buttons(context) -> None:  # noqa: F811
    pytest.importorskip("discord")
    from mflbot.discordbot.client import build_view

    actions = actions_for(context)
    rid = the_recommendation(context).id
    actions.reject(OWNER, rid)
    assert build_view(actions.card(rid)) is None


# ---------------------------------------------------------------------------
# the adapter's handling, with stand-ins for Discord's objects
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def defer(self, **kwargs):
        self.calls.append(("defer", kwargs))

    async def send_message(self, content, **kwargs):
        self.calls.append(("send_message", content, kwargs))

    async def send_modal(self, modal):
        self.calls.append(("send_modal", modal))


class FakeFollowup:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, content, **kwargs):
        self.sent.append(content)


class FakeMessage:
    def __init__(self, message_id: int = 22, channel_id: int = 11) -> None:
        from types import SimpleNamespace

        self.id = message_id
        self.channel = SimpleNamespace(id=channel_id)
        self.edits: list[dict] = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


def fake_interaction(user_id: str, message=None):
    from types import SimpleNamespace

    return SimpleNamespace(
        user=SimpleNamespace(id=int(user_id)),
        response=FakeResponse(),
        followup=FakeFollowup(),
        message=message,
    )


def test_a_strangers_click_is_refused_before_anything_runs(context, executed) -> None:  # noqa: F811
    pytest.importorskip("discord")
    import asyncio

    from mflbot.discordbot.client import MFLBotClient

    client = MFLBotClient(actions_for(context))
    rid = the_recommendation(context).id
    interaction = fake_interaction(STRANGER)

    asyncio.run(client.on_card_button(interaction, SUBMIT, rid))

    assert interaction.response.calls == [
        ("send_message", NOT_THE_OWNER, {"ephemeral": True})
    ]
    assert context.store.get(rid).status == RecommendationStatus.PROPOSED
    assert executed == []


def test_an_approve_click_decides_then_redraws_its_card(context) -> None:  # noqa: F811
    pytest.importorskip("discord")
    import asyncio

    from mflbot.discordbot.client import MFLBotClient

    client = MFLBotClient(actions_for(context))
    rid = the_recommendation(context).id
    message = FakeMessage()
    interaction = fake_interaction(OWNER, message)

    asyncio.run(client.on_card_button(interaction, APPROVE, rid))

    assert interaction.response.calls[0][0] == "defer", "Discord needs an answer in 3s"
    assert interaction.followup.sent[0].startswith(f"Approved {rid}")
    [edit] = message.edits
    assert [b.custom_id for b in edit["view"].children] == [
        f"mflbot:{SUBMIT}:{rid}", f"mflbot:{REJECT}:{rid}", f"mflbot:{EDIT}:{rid}",
    ]
    assert context.store.get(rid).status == RecommendationStatus.APPROVED
    assert publisher.load_card(context.repos, rid).key == "approved+live"


def test_a_reject_click_asks_for_a_note_first(context) -> None:  # noqa: F811
    pytest.importorskip("discord")
    import asyncio

    from mflbot.discordbot.client import MFLBotClient, RejectModal

    client = MFLBotClient(actions_for(context))
    rid = the_recommendation(context).id
    interaction = fake_interaction(OWNER, FakeMessage())

    asyncio.run(client.on_card_button(interaction, REJECT, rid))

    [(kind, modal)] = interaction.response.calls
    assert kind == "send_modal" and isinstance(modal, RejectModal)
    assert context.store.get(rid).status == RecommendationStatus.PROPOSED


def test_a_publishing_pass_posts_the_card_and_delivers_alerts(context) -> None:  # noqa: F811
    pytest.importorskip("discord")
    import asyncio
    from types import SimpleNamespace

    from mflbot.discordbot.client import MFLBotClient

    notifier = DiscordNotifier()
    notifier.attached = True
    notifier.send("mflbot has stalled", "details", urgent=True)
    client = MFLBotClient(actions_for(context), notifier=notifier)
    rid = the_recommendation(context).id
    sent = []

    async def send(content=None, **kwargs):
        sent.append((content, kwargs))
        return FakeMessage(message_id=100 + len(sent))

    dm = SimpleNamespace(id=11, send=send)

    async def owner_dm():
        return dm

    client.owner_dm = owner_dm
    asyncio.run(client.publish_loop.coro(client))

    card_post, alert = sent
    assert card_post[1]["embed"].footer.text.startswith(f"Recommendation {rid}")
    assert "mflbot has stalled" in alert[0]
    assert publisher.load_card(context.repos, rid).message_id == "101"
    # The next pass has nothing new to post.
    sent.clear()
    asyncio.run(client.publish_loop.coro(client))
    assert sent == []


def test_an_unexpected_error_still_gets_a_reply(context, monkeypatch) -> None:  # noqa: F811
    pytest.importorskip("discord")
    import asyncio

    from mflbot.discordbot.client import MFLBotClient

    actions = actions_for(context)
    rid = the_recommendation(context).id

    def broken(user_id, recommendation_id):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(actions, "approve", broken)
    client = MFLBotClient(actions)
    interaction = fake_interaction(OWNER, FakeMessage())

    asyncio.run(client.on_card_button(interaction, APPROVE, rid))

    [reply] = interaction.followup.sent
    assert "unexpected error" in reply and f"/show {rid}" in reply


def test_a_refused_dm_is_reported_once_not_every_pass(context, caplog) -> None:  # noqa: F811
    discord = pytest.importorskip("discord")
    import asyncio
    import logging
    from types import SimpleNamespace

    from mflbot.discordbot.client import MFLBotClient

    client = MFLBotClient(actions_for(context))
    the_recommendation(context)

    async def owner_dm():
        raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "50007")

    client.owner_dm = owner_dm
    with caplog.at_level(logging.ERROR, logger="mflbot.discordbot.client"):
        asyncio.run(client.publish_loop.coro(client))
        asyncio.run(client.publish_loop.coro(client))

    refusals = [r for r in caplog.records if "refused to deliver a DM" in r.getMessage()]
    assert len(refusals) == 1


def test_verbose_logging_keeps_discord_request_urls_out_of_the_log() -> None:
    import logging

    from mflbot.logging_setup import setup_logging

    root = logging.getLogger()
    saved = (root.level, list(root.handlers))
    try:
        setup_logging(verbose=True)
        # A reply's URL carries the interaction's token; discord.py logs
        # request URLs only at DEBUG.
        assert logging.getLogger("discord").getEffectiveLevel() >= logging.INFO
        assert logging.getLogger("discord.http").getEffectiveLevel() >= logging.INFO
    finally:
        root.setLevel(saved[0])
        root.handlers[:] = saved[1]
