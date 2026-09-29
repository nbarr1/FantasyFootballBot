"""Scheduler behaviour: catching up after downtime, planning lineup runs,
refreshing projections, and treating a broken news feed as a failure.

Everything here runs against fakes; nothing talks to MFL. All data is SYNTHETIC.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from mflbot.config import Config, LeagueRef
from mflbot.schedule.heartbeat import record_success

pytest.importorskip("apscheduler")

from mflbot.schedule import jobs as jobs_module  # noqa: E402


@pytest.fixture
def config() -> Config:
    return Config(league=LeagueRef("TEST0001", 2026, "example.invalid"))


class FakeContext:
    def __init__(self, repos, config, settings=None) -> None:
        self.repos = repos
        self.config = config
        self.notifier = SimpleNamespace(send=lambda *a, **k: True)
        self.settings = settings

    def league_settings(self):
        return self.settings


def build(repos, config, settings=None):
    runner = jobs_module.JobRunner(FakeContext(repos, config, settings))
    return runner, jobs_module.build_scheduler(runner, config)


def runs_now(scheduler, job_id: str) -> bool:
    when = getattr(scheduler.get_job(job_id), "next_run_time", None)
    return when is not None and abs((when - datetime.now(UTC)).total_seconds()) < 60


def test_overdue_jobs_run_at_startup_and_fresh_ones_wait(repos, config) -> None:
    now = datetime.now(UTC)
    record_success(repos, "config_refresh", now=now - timedelta(days=3))
    record_success(repos, "player_db_refresh", now=now - timedelta(hours=1))

    _, scheduler = build(repos, config)

    assert runs_now(scheduler, "config_refresh"), "three days stale must catch up now"
    assert not runs_now(scheduler, "player_db_refresh"), "fresh must wait for its slot"
    assert runs_now(scheduler, "league_state_poll"), "never run counts as overdue"
    assert not runs_now(scheduler, "heartbeat"), "the watchdog judges after catch-up"


def test_the_watchdog_measures_warm_up_from_scheduler_start(repos, config) -> None:
    runner, _ = build(repos, config)
    assert runner.started_at is not None
    assert runner._heartbeat.started_at == runner.started_at


def test_lineup_runs_are_replanned_not_duplicated(repos, config) -> None:
    first_deadline = datetime.now(UTC) + timedelta(days=3)
    settings = SimpleNamespace(lineup_deadline=first_deadline)
    _, scheduler = build(repos, config, settings)
    plan = scheduler.get_job("schedule_lineup_jobs").func

    plan()
    plan()  # the daily re-plan, with the deadline unchanged
    lineup_jobs = [j for j in scheduler.get_jobs()
                   if j.id.startswith(jobs_module.LINEUP_JOB_PREFIX)]
    assert len(lineup_jobs) == len(config.lineup.lead_times_hours)

    settings.lineup_deadline = first_deadline + timedelta(hours=1)
    plan()
    lineup_jobs = [j for j in scheduler.get_jobs()
                   if j.id.startswith(jobs_module.LINEUP_JOB_PREFIX)]
    assert len(lineup_jobs) == len(config.lineup.lead_times_hours), (
        "runs planned for the old deadline must be removed, not left to fire"
    )


def test_lineup_planning_runs_at_startup(repos, config) -> None:
    record_success(repos, "schedule_lineup_jobs")
    _, scheduler = build(repos, config)
    assert runs_now(scheduler, "schedule_lineup_jobs")


def test_projections_are_refreshed_for_this_week_and_next(monkeypatch, repos, config) -> None:
    synced = []
    monkeypatch.setattr(
        "mflbot.ingest.scores.sync_projections",
        lambda client, repos, week, force=False: synced.append((week, force)) or 10,
    )
    context = SimpleNamespace(client=None, repos=repos, current_week=lambda: 5)
    result = jobs_module.JobRunner(context).refresh_projections()
    assert synced == [(5, True), (6, True)]
    assert "week 5" in result


def test_no_projections_this_week_is_a_failed_refresh(monkeypatch, repos) -> None:
    monkeypatch.setattr(
        "mflbot.ingest.scores.sync_projections", lambda client, repos, week, force=False: 0
    )
    context = SimpleNamespace(client=None, repos=repos, current_week=lambda: 5)
    with pytest.raises(RuntimeError, match="no projections"):
        jobs_module.JobRunner(context).refresh_projections()


def test_a_failed_news_source_fails_the_job(monkeypatch, repos) -> None:
    closed = []

    class Source:
        source_id = "synthetic"

        def close(self):
            closed.append(True)

    monkeypatch.setattr(
        "mflbot.ingest.news.registry.build_sources", lambda *a, **k: [Source()]
    )
    monkeypatch.setattr(
        "mflbot.ingest.news.registry.ingest_news", lambda sources, repos: {"synthetic": -1}
    )
    context = SimpleNamespace(
        config=SimpleNamespace(news=SimpleNamespace(sources=("synthetic",))),
        client=SimpleNamespace(cache=None),
        repos=repos,
    )
    with pytest.raises(RuntimeError, match="synthetic"):
        jobs_module.JobRunner(context).ingest_news()
    assert closed == [True], "sources are closed even when the job fails"
