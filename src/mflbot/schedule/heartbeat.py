"""Making silence trustworthy.

This bot's central promise is that silence never causes an action. The cost of
that promise is that silence is also ambiguous: "no recommendations this week"
and "the machine lost power on Saturday" arrive looking identical -- as nothing
at all. A user who cannot tell those apart has to check manually, which is the
habit the bot exists to replace.

Two mechanisms, because there are two failure modes and neither one covers the
other:

**The bot is running, but its work is not.** MFL rejected the login, the network
is down, an ingest job has been throwing for six hours. The scheduler is alive,
so it can notice this itself: every job records the time it last succeeded, and
:func:`check` compares those against how often each job is *configured* to run.
Stale means stale relative to that job's real cadence, not a guessed constant.

**The bot is not running at all.** Power cut, OOM kill, someone closed the
laptop. Nothing in this process can report that, by definition -- code that is
not executing cannot send a message. That is what
:class:`~mflbot.notify.deadman.DeadManPing` is for: while healthy, the bot
checks in with an external service, and *stopping* is what raises the alarm.

The two compose into one rule, which is the reason the ping is sent from here
rather than from the scheduler loop directly:

    Ping only while healthy.

A dead man's switch that keeps checking in while the bot is failing is worse
than no switch at all -- it actively reassures you that a broken thing is fine.
So a stale job stops the pings, and the external monitor escalates on the same
signal it uses for a dead machine.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

log = logging.getLogger(__name__)

#: ingest_state key prefixes. Job outcomes live in the same table as the rest of
#: the ingest bookkeeping, so they survive a restart and are visible to
#: `bot status` without a second store.
LAST_SUCCESS = "job:last_success:"
LAST_FAILURE = "job:last_failure:"
LAST_ERROR = "job:last_error:"
ALERT_STATE = "heartbeat:alerted"


def record_success(repos, job_id: str, *, now: datetime | None = None) -> None:
    repos.set_state(LAST_SUCCESS + job_id, (now or datetime.now(UTC)).isoformat())


def record_failure(repos, job_id: str, error: str, *, now: datetime | None = None) -> None:
    repos.set_state(LAST_FAILURE + job_id, (now or datetime.now(UTC)).isoformat())
    # Truncated: this is a status line, not a log. The full traceback is in the
    # log, which the redacting formatter has already been through.
    repos.set_state(LAST_ERROR + job_id, error[:300])


def _read_time(repos, key: str) -> datetime | None:
    raw = repos.get_state(key)
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class MonitoredJob:
    """One job, and how long a gap is too long for it."""

    id: str
    label: str
    max_gap: timedelta

    def describe_gap(self) -> str:
        hours = self.max_gap.total_seconds() / 3600
        return f"{hours:.0f}h" if hours >= 1 else f"{self.max_gap.total_seconds() / 60:.0f}m"


def monitored_jobs(config, *, multiplier: float | None = None) -> tuple[MonitoredJob, ...]:
    """What to watch, with thresholds derived from this league's own cadences.

    The intervals come from ``config``, not from constants here: someone who
    polls every 15 minutes should hear about a stall sooner than someone who
    polls hourly, and neither should have to configure the watchdog separately
    from the thing it watches.
    """
    factor = multiplier if multiplier is not None else config.schedule.heartbeat_stale_multiplier
    poll = timedelta(minutes=config.schedule.league_state_poll_minutes * factor)
    news = timedelta(minutes=config.news.poll_minutes * factor)
    # The daily jobs run on a cron rather than an interval, so the threshold is
    # a day plus enough slack that a late run is not an alert.
    daily = timedelta(hours=26)
    return (
        MonitoredJob("league_state_poll", "League state poll", poll),
        MonitoredJob("news_ingest", "News ingestion", news),
        MonitoredJob("config_refresh", "Config refresh", daily),
        MonitoredJob("player_db_refresh", "Player database refresh", daily),
    )


@dataclass(frozen=True, slots=True)
class JobHealth:
    job: MonitoredJob
    last_success: datetime | None
    last_failure: datetime | None
    last_error: str
    age: timedelta | None
    stale: bool
    #: True when the job has never run and not enough time has passed to judge
    #: it -- a fresh install must not alert before its first cycle.
    warming_up: bool

    @property
    def state(self) -> str:
        if self.warming_up:
            return "waiting for first run"
        if self.stale:
            return "STALE"
        return "ok"

    def describe(self) -> str:
        if self.warming_up:
            return f"{self.job.label}: waiting for its first run"
        if self.last_success is None:
            return (
                f"{self.job.label}: has never completed, and more than "
                f"{self.job.describe_gap()} has passed since startup"
            )
        ago = _humanise(self.age)
        if self.stale:
            detail = f" (last error: {self.last_error})" if self.last_error else ""
            return (
                f"{self.job.label}: last succeeded {ago} ago, which is over its "
                f"{self.job.describe_gap()} threshold{detail}"
            )
        return f"{self.job.label}: last succeeded {ago} ago"


def _humanise(delta: timedelta | None) -> str:
    if delta is None:
        return "never"
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    return f"{seconds // 86400}d {(seconds % 86400) // 3600}h"


@dataclass(slots=True)
class HeartbeatReport:
    entries: list[JobHealth] = field(default_factory=list)
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def stale(self) -> list[JobHealth]:
        return [entry for entry in self.entries if entry.stale]

    @property
    def healthy(self) -> bool:
        return not self.stale

    def summary(self) -> str:
        if self.healthy:
            waiting = sum(1 for e in self.entries if e.warming_up)
            if waiting:
                return f"healthy ({waiting} job(s) awaiting a first run)"
            return "healthy"
        return f"{len(self.stale)} job(s) stale: " + ", ".join(
            entry.job.label for entry in self.stale
        )

    def render(self) -> str:
        lines = [f"Watchdog at {self.checked_at:%Y-%m-%d %H:%M UTC}: {self.summary()}"]
        lines.extend(f"  {entry.describe()}" for entry in self.entries)
        return "\n".join(lines)


def check(
    repos,
    config,
    *,
    started_at: datetime | None = None,
    now: datetime | None = None,
) -> HeartbeatReport:
    """Compare every monitored job against its own cadence.

    ``started_at`` is when this process began scheduling. Without it, a fresh
    install looks exactly like a broken one: no job has ever succeeded. With it,
    a job is only judged once the process has been up long enough for that job
    to have run at all.
    """
    now = now or datetime.now(UTC)
    entries = []
    for job in monitored_jobs(config):
        last_success = _read_time(repos, LAST_SUCCESS + job.id)
        age = None if last_success is None else now - last_success
        if last_success is None:
            uptime = None if started_at is None else now - started_at
            warming_up = uptime is None or uptime <= job.max_gap
            stale = not warming_up
        else:
            warming_up = False
            stale = age > job.max_gap
        entries.append(
            JobHealth(
                job=job,
                last_success=last_success,
                last_failure=_read_time(repos, LAST_FAILURE + job.id),
                last_error=repos.get_state(LAST_ERROR + job.id) or "",
                age=age,
                stale=stale,
                warming_up=warming_up,
            )
        )
    return HeartbeatReport(entries=entries, checked_at=now)


class Heartbeat:
    """The watchdog job: check, ping while healthy, alert on a change of state.

    Alerting is edge-triggered. A stall that has already been reported does not
    re-report every quarter of an hour -- an alert you learn to ignore is not an
    alert -- and recovery is announced once, so you know it is over without
    having to go and look.
    """

    def __init__(self, repos, config, notifier, ping=None, *, started_at=None) -> None:
        self._repos = repos
        self._config = config
        self._notifier = notifier
        self._ping = ping
        self.started_at = started_at or datetime.now(UTC)

    def run(self, *, now: datetime | None = None) -> HeartbeatReport:
        report = check(self._repos, self._config, started_at=self.started_at, now=now)
        already_alerted = self._repos.get_state(ALERT_STATE) == "1"

        if report.healthy:
            if self._ping is not None:
                self._ping.ping(report.summary())
            if already_alerted:
                self._notifier.send(
                    "mflbot is caught up again",
                    report.render(),
                )
                self._repos.set_state(ALERT_STATE, "0")
            return report

        # Deliberately no ping: withholding the check-in is what makes an
        # external monitor escalate, and a stalled bot deserves that escalation
        # exactly as much as a dead one.
        if not already_alerted:
            self._notifier.send(
                f"mflbot has stalled: {report.summary()}",
                report.render()
                + "\n\nNothing has been submitted, and nothing will be. This is a "
                  "warning that the bot may not be watching, not that it acted.",
                urgent=True,
            )
            self._repos.set_state(ALERT_STATE, "1")
        else:
            log.warning("watchdog still stale: %s", report.summary())
        return report
