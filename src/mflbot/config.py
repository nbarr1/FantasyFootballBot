"""Configuration loading.

Two kinds of settings live in two different places, deliberately:

* **Behaviour settings** (how large a projected gain must be before a waiver
  claim is worth surfacing, how many trade ideas to draft per week) live in
  ``config.toml``. These are the user's risk tolerance, not league data, so
  shipping conservative defaults for them is appropriate.
* **League data** (scoring rules, roster limits, lineup slots, waiver system,
  trade deadline) is *never* configured here. It is pulled from the MFL API and
  stored in the database. If it has not been pulled yet, dependent features
  block rather than fall back to an assumed default.

Secrets are never read from this file -- see :mod:`mflbot.mfl.auth`.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError

DEFAULT_CONFIG_PATH = Path("config.toml")


@dataclass(frozen=True, slots=True)
class LeagueRef:
    """Which league this bot instance watches. Supplied by the user."""

    id: str
    season: int
    host: str
    #: MFL franchise id of the *user's own* team, e.g. "0003". Required before
    #: any roster-relative analysis (lineup, waivers, trades) can run, and not
    #: guessable -- it is discovered by `bot whoami` once credentials exist.
    franchise_id: str | None = None

    @property
    def base_url(self) -> str:
        return f"https://{self.host}/{self.season}"

    @property
    def global_base_url(self) -> str:
        """Base URL for requests that take no league parameter (``L=``).

        MFL's developer docs are explicit: "if the request does not take a
        league parameter (L=), it must be sent to the host api" -- and doing so
        is also the documented best practice for spreading load ("use
        api.myfantasyleague.com ... that will spread out your requests across a
        number of servers"). The hostname is the literal ``api``, not derived
        from the configured league host.
        """
        return f"https://api.myfantasyleague.com/{self.season}"

    @property
    def api_docs_url(self) -> str:
        return f"https://{self.host}/{self.season}/api_info?STATE=details"


@dataclass(frozen=True, slots=True)
class StorageSettings:
    engine: str = "sqlite"
    path: str = "data/mflbot.db"
    #: Set when engine == "postgres". Left unimplemented; see README.
    dsn: str | None = None


@dataclass(frozen=True, slots=True)
class WaiverSettings:
    """Gates on *how many* add/drop ideas surface. Conservative by default."""

    #: Minimum projected points-per-week improvement over the roster player who
    #: would be dropped, before a claim is worth the user's attention.
    min_projection_delta: float = 1.5
    #: Rest-of-season projected improvement threshold, used for stash-type adds.
    min_ros_delta: float = 8.0
    #: Never surface more than this many add/drop pairs in one batch.
    max_recommendations: int = 5
    #: Free agents projected below this are not even considered, to keep the
    #: candidate pool tractable.
    candidate_projection_floor: float = 4.0
    #: Fraction of remaining blind-bid budget to offer for the top-value claim.
    #: 0.0 = always bid the minimum, 1.0 = shoot the whole budget.
    bbid_aggressiveness: float = 0.15


@dataclass(frozen=True, slots=True)
class TradeSettings:
    max_proposals_per_week: int = 3
    #: A proposal is only surfaced if it projects as a gain for *both* sides by
    #: at least this many rest-of-season points -- a trade the other manager
    #: would obviously refuse is noise.
    min_mutual_gain: float = 5.0
    #: How much projected value the user must gain for an incoming offer to be
    #: recommended for acceptance.
    min_accept_gain: float = 3.0


@dataclass(frozen=True, slots=True)
class LineupSettings:
    #: Hours before the league's lineup deadline at which to run analysis. The
    #: deadline itself is read from the league export, never assumed.
    lead_times_hours: tuple[float, ...] = (48.0, 12.0, 2.0)
    #: Escalate loudly if a player designated OUT/IR is still starting inside
    #: this many hours of lock.
    escalate_within_hours: float = 3.0


@dataclass(frozen=True, slots=True)
class NewsSettings:
    #: Adapter ids to enable. "mfl_injuries" is the always-on baseline;
    #: "sleeper" is the default free, no-credential external source.
    sources: tuple[str, ...] = ("mfl_injuries", "sleeper")
    poll_minutes: int = 45


@dataclass(frozen=True, slots=True)
class NotifySettings:
    #: Transport id: "webhook" (works with a Discord channel webhook),
    #: "discord" (DMs from the Discord bot; needs [discord] enabled), "email",
    #: "telegram".
    transport: str = "webhook"
    #: Webhook URL is a secret and is read from MFLBOT_WEBHOOK_URL, not here.
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class WebSettings:
    """The dashboard (``bot serve``). Behaviour only -- the password is a secret
    and comes from ``MFLBOT_WEB_PASSWORD``, never from this file."""

    host: str = "127.0.0.1"
    port: int = 8765
    #: How long a signed-in browser session lasts before it must sign in again.
    session_ttl_minutes: int = 720
    #: When false, the dashboard records approvals but no web request can cause
    #: an MFL write; `bot execute` submits them instead.
    allow_submissions: bool = True
    #: When false, the ingestion/analysis buttons are removed and the dashboard
    #: is a viewer over whatever the scheduler produced.
    allow_jobs: bool = True


@dataclass(frozen=True, slots=True)
class DiscordSettings:
    """The Discord bot (``bot run`` / ``bot serve``). Behaviour only -- the bot
    token is a secret and comes from ``MFLBOT_DISCORD_TOKEN``, never from this
    file."""

    enabled: bool = False
    #: Your Discord user id (Discord: Settings > Advanced > Developer Mode, then
    #: right-click your name > Copy User ID). The bot DMs this account, and only
    #: this account can approve, reject, edit or submit.
    owner_user_id: str = ""
    #: When false, Discord can approve, reject and edit, but submitting stays
    #: with the dashboard or `bot execute`.
    allow_submissions: bool = True
    #: How often the bot posts new recommendations and updates decided ones.
    publish_seconds: int = 30


@dataclass(frozen=True, slots=True)
class ScheduleSettings:
    league_state_poll_minutes: int = 45
    config_refresh_cron: str = "0 5 * * *"
    player_db_refresh_cron: str = "30 5 * * *"
    #: This week's and next week's projections. After the config refresh, so a
    #: week rollover is picked up the same morning.
    projections_refresh_cron: str = "45 5 * * *"
    waiver_analysis_cron: str = "0 22 * * 1"
    trade_analysis_cron: str = "0 20 * * 3"
    #: How often the watchdog checks whether the other jobs are keeping up, and
    #: checks in with MFLBOT_HEARTBEAT_URL while they are.
    heartbeat_minutes: int = 15
    #: A job counts as stalled once this many of its own intervals have passed
    #: without a success. Above 1 so that a single transient failure -- one
    #: refused request, one flaky minute of network -- is not an alert.
    heartbeat_stale_multiplier: float = 3.0


@dataclass(frozen=True, slots=True)
class Config:
    league: LeagueRef
    storage: StorageSettings = field(default_factory=StorageSettings)
    waivers: WaiverSettings = field(default_factory=WaiverSettings)
    trades: TradeSettings = field(default_factory=TradeSettings)
    lineup: LineupSettings = field(default_factory=LineupSettings)
    news: NewsSettings = field(default_factory=NewsSettings)
    notify: NotifySettings = field(default_factory=NotifySettings)
    schedule: ScheduleSettings = field(default_factory=ScheduleSettings)
    web: WebSettings = field(default_factory=WebSettings)
    discord: DiscordSettings = field(default_factory=DiscordSettings)
    #: Approval channel id: "cli", "web" or "discord". All enforce the same
    #: rules; this only selects which one the scheduler's notifications point at.
    approval_channel: str = "cli"
    #: The directory holding config.toml. Relative paths the bot keeps state
    #: in -- the database, endpoints.lock.json, the approval signing key, the
    #: response cache -- resolve against this, not the working directory, so
    #: running `bot` from somewhere else cannot quietly pick up different
    #: state, or a different set of verified write endpoints.
    base_dir: Path = field(default_factory=lambda: Path("."))

    def resolve(self, path: Path | str) -> Path:
        """``path`` if absolute, else relative to :attr:`base_dir`."""
        path = Path(path)
        return path if path.is_absolute() else self.base_dir / path


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a table")
    return value


def _build(cls: type, data: dict[str, Any], section: str) -> Any:
    fields = {f for f in cls.__annotations__}
    unknown = set(data) - fields
    if unknown:
        raise ConfigError(
            f"[{section}] has unknown key(s): {', '.join(sorted(unknown))}"
        )
    # tuple-typed fields arrive from TOML as lists.
    coerced = {k: (tuple(v) if isinstance(v, list) else v) for k, v in data.items()}
    return cls(**coerced)


def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> Config:
    """Load and validate ``config.toml``.

    Raises :class:`ConfigError` rather than returning partial defaults, because
    a silently half-configured bot is worse than one that will not start.
    """
    path = Path(path)
    if not path.exists():
        raise ConfigError(
            f"No config file at {path}. Copy config.example.toml to {path} "
            "and fill in the [league] section."
        )
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc

    league_raw = _section(raw, "league")
    for required in ("id", "season", "host"):
        if required not in league_raw:
            raise ConfigError(f"[league] is missing required key '{required}'")
    league = _build(LeagueRef, league_raw, "league")

    config = Config(
        league=league,
        storage=_build(StorageSettings, _section(raw, "storage"), "storage"),
        waivers=_build(WaiverSettings, _section(raw, "waivers"), "waivers"),
        trades=_build(TradeSettings, _section(raw, "trades"), "trades"),
        lineup=_build(LineupSettings, _section(raw, "lineup"), "lineup"),
        news=_build(NewsSettings, _section(raw, "news"), "news"),
        notify=_build(NotifySettings, _section(raw, "notify"), "notify"),
        schedule=_build(ScheduleSettings, _section(raw, "schedule"), "schedule"),
        web=_build(WebSettings, _section(raw, "web"), "web"),
        discord=_discord(_section(raw, "discord")),
        approval_channel=_approval_channel(raw.get("approval_channel", "cli")),
        base_dir=path.resolve().parent,
    )
    _check_discord_references(config)
    return config


def _approval_channel(value: Any) -> str:
    if value not in ("cli", "web", "discord"):
        raise ConfigError(
            f"approval_channel must be \"cli\", \"web\" or \"discord\", not {value!r}"
        )
    return value


def _discord(data: dict[str, Any]) -> DiscordSettings:
    # A Discord id is a large integer, and TOML reads a bare one as a number.
    if isinstance(data.get("owner_user_id"), int):
        data = {**data, "owner_user_id": str(data["owner_user_id"])}
    settings = _build(DiscordSettings, data, "discord")
    owner = settings.owner_user_id.strip()
    if settings.enabled and not owner:
        raise ConfigError(
            "[discord] is enabled but owner_user_id is not set. There is no mode in "
            "which anyone who can see the bot's messages can approve MFL writes."
        )
    if owner and not owner.isdigit():
        raise ConfigError(
            f"[discord] owner_user_id must be a Discord user id (digits only), not "
            f"{settings.owner_user_id!r}"
        )
    if settings.publish_seconds < 5:
        raise ConfigError(
            f"[discord] publish_seconds must be at least 5, not {settings.publish_seconds!r}"
        )
    return settings


def _check_discord_references(config: Config) -> None:
    """Refuse settings that point at a Discord bot that will never run: every
    notification would be logged instead of sent, and the approval hint would
    send you to buttons that were never posted."""
    if config.discord.enabled:
        return
    if config.notify.enabled and config.notify.transport == "discord":
        raise ConfigError(
            '[notify] transport is "discord" but [discord] is not enabled. Enable '
            "[discord], or choose another transport."
        )
    if config.approval_channel == "discord":
        raise ConfigError(
            'approval_channel is "discord" but [discord] is not enabled. Enable '
            '[discord], or set approval_channel to "cli" or "web".'
        )
