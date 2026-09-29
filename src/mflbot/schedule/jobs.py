"""Scheduled jobs and the runner that executes them.

Cadences follow MFL's guidance and the league's own calendar:

===========================  ==========================================
Job                          When
===========================  ==========================================
Config refresh               daily (catches mid-season scoring edits)
Player database refresh      daily (MFL's stated limit)
Projections refresh          daily, this week and next
League-state diff            every 30-60 minutes in season
News ingestion               every 30-60 minutes
Waiver analysis              weekly, plus on any roster/free-agent diff
Trade proposals              weekly
Trade offers to you          every poll (each offer is evaluated once)
Lineup analysis              computed from the league's real deadline
===========================  ==========================================

Analysis jobs produce recommendations and notify. **No job executes anything.**
The scheduler has no access to a write client; execution happens only when a
human approves through :mod:`mflbot.approval`.

Every job's outcome is recorded as it runs, which is what
:mod:`mflbot.schedule.heartbeat` reads to tell a quiet week apart from a
stopped bot. On startup, any job whose last success is older than its own
cadence runs straight away rather than at its next slot, so a restart after
downtime catches up instead of reporting stale for up to a day.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

log = logging.getLogger(__name__)

#: Id prefix for the one-off lineup-analysis runs planned from the deadline.
LINEUP_JOB_PREFIX = "lineup_analysis_"


@dataclass(slots=True)
class JobRunner:
    """Holds the wiring each job needs and exposes them as callables."""

    context: object  # mflbot.cli.BotContext
    #: When the scheduler started. The watchdog judges never-run jobs against
    #: this, so it is set once, when the scheduler is built.
    started_at: datetime | None = None
    #: Built on first use so that constructing a JobRunner stays free of I/O.
    _heartbeat_impl: object = None

    @property
    def _heartbeat(self):
        if self._heartbeat_impl is None:
            from ..notify.deadman import DeadManPing
            from .heartbeat import Heartbeat

            self._heartbeat_impl = Heartbeat(
                self.context.repos,
                self.context.config,
                self.context.notifier,
                ping=DeadManPing(),
                started_at=self.started_at,
            )
        return self._heartbeat_impl

    # -- ingestion ---------------------------------------------------------

    def refresh_config(self) -> str:
        from ..ingest.config_sync import sync_config

        result = sync_config(
            self.context.client, self.context.repos, force=True
        )
        if result.blocked:
            self.context.notifier.send(
                "League configuration refreshed with gaps",
                "\n".join(b.describe() for b in result.blocked),
            )
        return f"config refreshed; {len(result.blocked)} feature(s) blocked"

    def refresh_players(self) -> str:
        from ..ingest.players import sync_players

        count = sync_players(self.context.client, self.context.repos)
        return f"player database: {count} rows"

    def poll_state(self) -> str:
        from ..ingest.league_state import poll_league_state

        # Forced past the response cache: the cache's TTL for rosters and free
        # agents is an hour, so an unforced poll could not notice a change any
        # sooner than that, however often [schedule] asks it to run.
        diff = poll_league_state(self.context.client, self.context.repos, force=True)
        if not diff.has_changes:
            return "no league changes"

        # A change is a trigger, not an action.
        if diff.roster_changed or diff.free_agents_changed:
            self.analyse_waivers()
        return diff.summary()

    def check_offers(self) -> str:
        """Evaluate trade offers made to you. Each offer is handled once."""
        return self.context.run_offer_analysis()

    def refresh_projections(self) -> str:
        """This week's projections and next week's, where MFL publishes them.

        Every analysis engine reads stored projections, so without this job a
        running bot would analyse whatever someone last synced by hand.
        """
        from ..ingest.scores import sync_projections

        week = self.context.current_week()
        if week is None:
            raise RuntimeError("the current NFL week could not be determined")
        counts = {
            w: sync_projections(self.context.client, self.context.repos, w, force=True)
            for w in (week, week + 1)
        }
        # No projections is reported, not raised. Some MFL hosts publish none;
        # the analysis engines already block on that and say why, and a job
        # that failed every day for it would silence the dead man's ping for
        # good -- an alarm for a data gap rather than a stalled bot.
        return ", ".join(
            f"week {w}: {n} rows" if n else f"week {w}: MFL published none"
            for w, n in counts.items()
        )

    def ingest_news(self) -> str:
        from ..ingest.news.registry import build_sources, ingest_news

        sources = build_sources(
            self.context.config.news.sources,
            self.context.client,
            self.context.repos,
            self.context.client.cache,
        )
        try:
            results = ingest_news(sources, self.context.repos)
        finally:
            for source in sources:
                source.close()
        summary = ", ".join(f"{k}={v}" for k, v in results.items()) or "no sources enabled"
        failed = sorted(k for k, v in results.items() if v < 0)
        if failed:
            # A broken feed looks exactly like a quiet one unless it is
            # reported as a failure, so the watchdog can see it.
            raise RuntimeError(f"news source(s) failed: {', '.join(failed)} ({summary})")
        return summary

    # -- analysis ----------------------------------------------------------

    def analyse_waivers(self) -> str:
        return self.context.run_waiver_analysis()

    def analyse_trades(self) -> str:
        return self.context.run_trade_analysis()

    def analyse_lineup(self) -> str:
        return self.context.run_lineup_analysis()

    def expire_recommendations(self) -> str:
        count = self.context.store.expire_stale()
        return f"{count} recommendation(s) expired"

    def heartbeat(self) -> str:
        """Check that the other jobs are keeping up; check in while they are."""
        return self._heartbeat.run().summary()


def lineup_run_times(
    deadline: datetime, lead_times_hours: tuple[float, ...], now: datetime | None = None
) -> list[datetime]:
    """When to run lineup analysis, derived from the league's real deadline.

    The deadline comes from the league export. Nothing here assumes Sunday
    morning, or any particular day.
    """
    now = now or datetime.now(UTC)
    return sorted(
        t
        for t in (deadline - timedelta(hours=h) for h in lead_times_hours)
        if t > now
    )


def _overdue(repos, job_id: str, cadence: timedelta, now: datetime) -> bool:
    """True when a job has not succeeded within one of its own cadences."""
    from .heartbeat import LAST_SUCCESS, _read_time

    last = _read_time(repos, LAST_SUCCESS + job_id)
    return last is None or now - last > cadence


def build_scheduler(runner: JobRunner, config):
    """Build an APScheduler instance with every job registered."""
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.interval import IntervalTrigger
    except ImportError as exc:  # pragma: no cover
        raise ImportError("APScheduler is required: pip install apscheduler") from exc

    from .heartbeat import record_failure, record_success

    scheduler = BackgroundScheduler(timezone="UTC")
    now = datetime.now(UTC)
    runner.started_at = runner.started_at or now
    repos = runner.context.repos

    def recorded(name: str, func: Callable[[], str]) -> Callable[[], None]:
        # Every outcome is recorded here rather than in each job, so a job
        # added later is watched by the heartbeat without anyone remembering
        # to instrument it.
        def wrapped() -> None:
            try:
                result = func()
                log.info("job %s: %s", name, result)
                record_success(repos, name)
            except Exception as exc:  # noqa: BLE001 - one bad job must not kill the loop
                log.exception("job %s failed", name)
                record_failure(repos, name, f"{type(exc).__name__}: {exc}")

        return wrapped

    def register(name: str, func: Callable[[], str], trigger, *,
                 catch_up_after: timedelta | None = None) -> None:
        extra = {}
        if catch_up_after is not None and _overdue(repos, name, catch_up_after, now):
            extra["next_run_time"] = now
        scheduler.add_job(recorded(name, func), trigger, id=name, name=name,
                          replace_existing=True, max_instances=1, coalesce=True, **extra)

    schedule = config.schedule
    daily = timedelta(days=1)
    poll = timedelta(minutes=schedule.league_state_poll_minutes)
    register("config_refresh", runner.refresh_config,
             CronTrigger.from_crontab(schedule.config_refresh_cron, timezone="UTC"),
             catch_up_after=daily)
    register("player_db_refresh", runner.refresh_players,
             CronTrigger.from_crontab(schedule.player_db_refresh_cron, timezone="UTC"),
             catch_up_after=daily)
    register("projections_refresh", runner.refresh_projections,
             CronTrigger.from_crontab(schedule.projections_refresh_cron, timezone="UTC"),
             catch_up_after=daily)
    register("league_state_poll", runner.poll_state,
             IntervalTrigger(minutes=schedule.league_state_poll_minutes),
             catch_up_after=poll)
    register("trade_offers", runner.check_offers,
             IntervalTrigger(minutes=schedule.league_state_poll_minutes),
             catch_up_after=poll)
    register("news_ingest", runner.ingest_news,
             IntervalTrigger(minutes=config.news.poll_minutes),
             catch_up_after=timedelta(minutes=config.news.poll_minutes))
    register("waiver_analysis", runner.analyse_waivers,
             CronTrigger.from_crontab(schedule.waiver_analysis_cron, timezone="UTC"))
    register("trade_analysis", runner.analyse_trades,
             CronTrigger.from_crontab(schedule.trade_analysis_cron, timezone="UTC"))
    register("expire_recommendations", runner.expire_recommendations,
             IntervalTrigger(minutes=15))
    # The watchdog is registered like any other job, so a stall in the watchdog
    # itself shows up in the same place as any other stalled job. It is not
    # caught up at startup: it should judge after the catch-up runs, not race them.
    register("heartbeat", runner.heartbeat,
             IntervalTrigger(minutes=schedule.heartbeat_minutes))

    # Lineup analysis is scheduled relative to the league's actual deadline,
    # which is only known once the config has been synced. This re-plans the
    # runs each day (and at startup) as the deadline moves week to week.
    def schedule_lineup_jobs() -> str:
        settings = runner.context.league_settings()
        deadline = settings.lineup_deadline if settings is not None else None
        times = lineup_run_times(deadline, config.lineup.lead_times_hours) if deadline else []
        # Ids come from the run time itself, so re-planning replaces a run
        # rather than stacking a duplicate beside it; runs no longer planned
        # are removed.
        wanted = {f"{LINEUP_JOB_PREFIX}{t:%Y%m%dT%H%M}": t for t in times}
        for job in scheduler.get_jobs():
            if job.id.startswith(LINEUP_JOB_PREFIX) and job.id not in wanted:
                job.remove()
        for job_id, run_at in wanted.items():
            if scheduler.get_job(job_id) is not None:
                continue  # already planned for this exact time
            scheduler.add_job(
                recorded("lineup_analysis", runner.analyse_lineup),
                "date",
                run_date=run_at,
                id=job_id,
                name=job_id,
            )
        if deadline is None:
            return "lineup deadline unknown; no lineup jobs scheduled"
        return f"scheduled {len(times)} lineup run(s)"

    register("schedule_lineup_jobs", schedule_lineup_jobs,
             CronTrigger.from_crontab("15 5 * * *", timezone="UTC"),
             catch_up_after=timedelta(0))

    return scheduler
