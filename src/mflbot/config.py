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
    #: Transport id: "webhook" (works with Discord), "email", "telegram".
    transport: str = "webhook"
    #: Webhook URL is a secret and is read from MFLBOT_WEBHOOK_URL, not here.
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class ScheduleSettings:
    league_state_poll_minutes: int = 45
    config_refresh_cron: str = "0 5 * * *"
    player_db_refresh_cron: str = "30 5 * * *"
    waiver_analysis_cron: str = "0 22 * * 1"
    trade_analysis_cron: str = "0 20 * * 3"


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
    #: Approval channel id: "cli" (default) or "web" (scaffolded dashboard).
    approval_channel: str = "cli"


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

    return Config(
        league=league,
        storage=_build(StorageSettings, _section(raw, "storage"), "storage"),
        waivers=_build(WaiverSettings, _section(raw, "waivers"), "waivers"),
        trades=_build(TradeSettings, _section(raw, "trades"), "trades"),
        lineup=_build(LineupSettings, _section(raw, "lineup"), "lineup"),
        news=_build(NewsSettings, _section(raw, "news"), "news"),
        notify=_build(NotifySettings, _section(raw, "notify"), "notify"),
        schedule=_build(ScheduleSettings, _section(raw, "schedule"), "schedule"),
        approval_channel=raw.get("approval_channel", "cli"),
    )
