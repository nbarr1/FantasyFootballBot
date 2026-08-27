"""The watchdog: telling a quiet week apart from a stopped bot.

The properties under test are the ones that decide whether the heartbeat is
worth having at all:

* A stalled bot stops checking in. A dead man's switch that keeps reassuring an
  external monitor while the bot is broken is worse than no switch.
* An alert fires on the transition, not on every check. An alert you learn to
  ignore has stopped being an alert.
* A fresh install is not an alarm. Nothing has run yet, and that is correct.
* Thresholds come from the league's own configured cadences, so a bot that
  polls every 15 minutes is judged faster than one that polls hourly.
* The check-in URL never reaches a log. Its secret is in the path, where the
  redactor cannot reach it.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest

from mflbot.config import Config, LeagueRef, NewsSettings, ScheduleSettings
from mflbot.notify.deadman import DeadManPing
from mflbot.schedule.heartbeat import (
    ALERT_STATE,
    Heartbeat,
    check,
    monitored_jobs,
    record_failure,
    record_success,
)

NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


@pytest.fixture
def config() -> Config:
    return Config(
        league=LeagueRef("TEST0001", 2026, "example.invalid"),
        schedule=ScheduleSettings(league_state_poll_minutes=45),
        news=NewsSettings(poll_minutes=45),
    )


class RecordingNotifier:
    transport_id = "recording"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, bool]] = []

    def send(self, subject: str, body: str, *, urgent: bool = False) -> bool:
        self.sent.append((subject, body, urgent))
        return True

    def is_configured(self):
        return True, ""


class RecordingPing:
    def __init__(self) -> None:
        self.pings: list[str] = []

    def ping(self, message: str = "") -> bool:
        self.pings.append(message)
        return True


def all_fresh(repos, config, at: datetime) -> None:
    for job in monitored_jobs(config):
        record_success(repos, job.id, now=at)


# ---------------------------------------------------------------------------
# what counts as stale
# ---------------------------------------------------------------------------

def test_thresholds_come_from_the_configured_cadence() -> None:
    brisk = Config(
        league=LeagueRef("L", 2026, "h"),
        schedule=ScheduleSettings(league_state_poll_minutes=15),
        news=NewsSettings(poll_minutes=15),
    )
    relaxed = Config(
        league=LeagueRef("L", 2026, "h"),
        schedule=ScheduleSettings(league_state_poll_minutes=60),
        news=NewsSettings(poll_minutes=60),
    )
    gap = {j.id: j.max_gap for j in monitored_jobs(brisk)}["league_state_poll"]
    slower = {j.id: j.max_gap for j in monitored_jobs(relaxed)}["league_state_poll"]
    assert gap == timedelta(minutes=45)      # 15 * 3
    assert slower == timedelta(minutes=180)  # 60 * 3
    assert gap < slower


def test_a_recent_success_is_healthy(repos, config) -> None:
    all_fresh(repos, config, NOW - timedelta(minutes=5))
    report = check(repos, config, started_at=NOW - timedelta(days=1), now=NOW)
    assert report.healthy
    assert report.summary() == "healthy"


def test_a_job_past_its_threshold_is_stale(repos, config) -> None:
    all_fresh(repos, config, NOW - timedelta(minutes=5))
    record_success(repos, "league_state_poll", now=NOW - timedelta(hours=4))
    report = check(repos, config, started_at=NOW - timedelta(days=1), now=NOW)

    assert not report.healthy
    assert [entry.job.id for entry in report.stale] == ["league_state_poll"]
    assert "League state poll" in report.summary()


def test_a_fresh_install_is_not_an_alarm(repos, config) -> None:
    """Nothing has run yet, which is correct, not broken."""
    report = check(repos, config, started_at=NOW - timedelta(minutes=1), now=NOW)
    assert report.healthy
    assert all(entry.warming_up for entry in report.entries)
    assert "awaiting a first run" in report.summary()


def test_a_job_that_never_runs_eventually_is_an_alarm(repos, config) -> None:
    report = check(repos, config, started_at=NOW - timedelta(days=3), now=NOW)
    assert not report.healthy
    assert "has never completed" in report.render()


def test_the_last_error_is_reported_with_the_stall(repos, config) -> None:
    all_fresh(repos, config, NOW - timedelta(minutes=5))
    record_success(repos, "news_ingest", now=NOW - timedelta(hours=6))
    record_failure(repos, "news_ingest", "AuthError: login refused", now=NOW)
    report = check(repos, config, started_at=NOW - timedelta(days=1), now=NOW)
    assert "AuthError: login refused" in report.render()


# ---------------------------------------------------------------------------
# the dead man's switch
# ---------------------------------------------------------------------------

def test_a_healthy_bot_checks_in(repos, config) -> None:
    all_fresh(repos, config, NOW - timedelta(minutes=5))
    ping = RecordingPing()
    Heartbeat(repos, config, RecordingNotifier(), ping,
              started_at=NOW - timedelta(days=1)).run(now=NOW)
    assert ping.pings == ["healthy"]


def test_a_stalled_bot_stops_checking_in(repos, config) -> None:
    """The whole point: absence of a check-in is the alarm."""
    all_fresh(repos, config, NOW - timedelta(minutes=5))
    record_success(repos, "league_state_poll", now=NOW - timedelta(hours=9))
    ping = RecordingPing()
    Heartbeat(repos, config, RecordingNotifier(), ping,
              started_at=NOW - timedelta(days=1)).run(now=NOW)
    assert ping.pings == [], "a stalled bot must not reassure its monitor"


def test_the_check_in_url_never_reaches_a_log(caplog) -> None:
    """The secret is in the path, where the URL redactor cannot reach it."""
    secret = "https://hc-ping.com/9f3a-super-secret-uuid"

    class Exploding:
        def post(self, *args, **kwargs):
            raise RuntimeError(f"connection to {secret} failed")

    pinger = DeadManPing(secret, http=Exploding())
    with caplog.at_level(logging.DEBUG):
        assert pinger.ping("healthy") is False

    assert "hc-ping.com" in caplog.text, "the host is quotable and useful"
    assert "9f3a-super-secret-uuid" not in caplog.text
    assert pinger.safe_target == "hc-ping.com"
    assert secret not in pinger.safe_target


def test_a_failing_monitor_never_breaks_the_scheduler() -> None:
    class Exploding:
        def post(self, *args, **kwargs):
            raise OSError("network unreachable")

    assert DeadManPing("https://example.invalid/x", http=Exploding()).ping() is False


# ---------------------------------------------------------------------------
# alerting behaviour
# ---------------------------------------------------------------------------

def test_a_stall_alerts_once_not_every_cycle(repos, config) -> None:
    all_fresh(repos, config, NOW - timedelta(minutes=5))
    record_success(repos, "league_state_poll", now=NOW - timedelta(hours=9))
    notifier = RecordingNotifier()
    beat = Heartbeat(repos, config, notifier, RecordingPing(),
                     started_at=NOW - timedelta(days=1))

    beat.run(now=NOW)
    beat.run(now=NOW + timedelta(minutes=15))
    beat.run(now=NOW + timedelta(minutes=30))

    assert len(notifier.sent) == 1
    subject, body, urgent = notifier.sent[0]
    assert "stalled" in subject
    assert urgent is True
    # The alert must not read as though the bot did something.
    assert "Nothing has been submitted" in body


def test_recovery_is_announced_once(repos, config) -> None:
    all_fresh(repos, config, NOW - timedelta(minutes=5))
    record_success(repos, "league_state_poll", now=NOW - timedelta(hours=9))
    notifier = RecordingNotifier()
    ping = RecordingPing()
    beat = Heartbeat(repos, config, notifier, ping, started_at=NOW - timedelta(days=1))

    beat.run(now=NOW)
    assert repos.get_state(ALERT_STATE) == "1"

    all_fresh(repos, config, NOW + timedelta(minutes=10))
    beat.run(now=NOW + timedelta(minutes=15))
    beat.run(now=NOW + timedelta(minutes=30))

    subjects = [subject for subject, _, _ in notifier.sent]
    assert subjects == [subjects[0], "mflbot is caught up again"]
    assert repos.get_state(ALERT_STATE) == "0"
    assert len(ping.pings) == 2, "check-ins resume the moment it is healthy again"


# ---------------------------------------------------------------------------
# the scheduler records what the watchdog reads
# ---------------------------------------------------------------------------

def test_the_scheduler_records_every_job_outcome(repos, config, monkeypatch) -> None:
    """Instrumentation lives in the scheduler's wrapper, not in each job.

    A job added later is therefore watched without anyone remembering to
    instrument it -- exactly the kind of thing that gets forgotten.
    """
    pytest.importorskip("apscheduler")
    from mflbot.schedule import jobs as jobs_module
    from mflbot.schedule.heartbeat import LAST_ERROR, LAST_FAILURE, LAST_SUCCESS

    class FakeContext:
        def __init__(self, repos, config) -> None:
            self.repos = repos
            self.config = config
            self.notifier = RecordingNotifier()

        def league_settings(self):
            return None

    monkeypatch.setattr(
        jobs_module.JobRunner, "poll_state", lambda self: "polled", raising=False
    )
    monkeypatch.setattr(
        jobs_module.JobRunner,
        "ingest_news",
        lambda self: (_ for _ in ()).throw(RuntimeError("MFL said no")),
        raising=False,
    )

    runner = jobs_module.JobRunner(FakeContext(repos, config))
    scheduler = jobs_module.build_scheduler(runner, config)

    # The registered callable is the instrumented wrapper, so calling it is
    # exactly what the scheduler does when the trigger fires.
    scheduler.get_job("league_state_poll").func()
    assert repos.get_state(LAST_SUCCESS + "league_state_poll") is not None

    scheduler.get_job("news_ingest").func()  # raises inside; must not propagate
    assert repos.get_state(LAST_FAILURE + "news_ingest") is not None
    assert "MFL said no" in repos.get_state(LAST_ERROR + "news_ingest")
    assert repos.get_state(LAST_SUCCESS + "news_ingest") is None

    assert scheduler.get_job("heartbeat") is not None, "the watchdog is itself a job"


def test_the_watchdog_cannot_reach_a_write_client() -> None:
    """The heartbeat is monitoring, not an actor.

    Stated as an import-graph property for the same reason the analysis
    packages are: a future edit that reaches for the write client here should
    fail a test rather than a review.
    """
    import ast
    import pathlib as _pathlib

    source = _pathlib.Path("src/mflbot/schedule/heartbeat.py")
    if not source.exists():  # installed rather than checked out
        import mflbot.schedule.heartbeat as module

        source = _pathlib.Path(module.__file__)

    tree = ast.parse(source.read_text(encoding="utf-8"))
    imported = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert not any("write_client" in name for name in imported)
    assert not any("execute" in name for name in imported)
