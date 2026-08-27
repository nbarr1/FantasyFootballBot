"""Scheduled jobs and the runner that executes them.

Cadences follow MFL's guidance and the league's own calendar:

===========================  ==========================================
Job                          When
===========================  ==========================================
Config refresh               daily (catches mid-season scoring edits)
Player database refresh      daily (MFL's stated limit)
League-state diff            every 30-60 minutes in season
News ingestion               every 30-60 minutes
Waiver analysis              weekly, plus on any roster/free-agent diff
Trade analysis               weekly, plus on an incoming offer
Lineup analysis              computed from the league's real deadline
Live scoring watch           inside NFL game windows only
===========================  ==========================================

Analysis jobs produce recommendations and notify. **No job executes anything.**
The scheduler has no access to a write client; execution happens only when a
human approves through :mod:`mflbot.approval`.

Every job's outcome is recorded as it runs, which is what
:mod:`mflbot.schedule.heartbeat` reads to tell a quiet week apart from a
stopped bot.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

log = logging.getLogger(__name__)


@dataclass(slots=True)
class JobRunner:
    """Holds the wiring each job needs and exposes them as callables."""

    context: object  # mflbot.cli.BotContext
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

        diff = poll_league_state(self.context.client, self.context.repos)
        if not diff.has_changes:
            return "no league changes"

        # A change is a trigger, not an action.
        if diff.roster_changed or diff.free_agents_changed:
            self.analyse_waivers()
        offers = diff.incoming_trades(self.context.config.league.franchise_id)
        if offers:
            self.analyse_trades()
        return diff.summary()

    def ingest_news(self) -> str:
        from ..ingest.news.registry import build_sources, ingest_news

        sources = build_sources(
            self.context.config.news.sources,
            self.context.client,
            self.context.repos,
            self.context.client.cache,
        )
        results = ingest_news(sources, self.context.repos)
        for source in sources:
            source.close()
        return ", ".join(f"{k}={v}" for k, v in results.items()) or "no sources enabled"

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

    def watch_live_scoring(self) -> str:
        """Poll live scoring during a game window.

        In-game information is treated as a *next week* signal. A player going
        quiet mid-game is not a reason to act now -- lineups are already locked --
        so this job records and never proposes.
        """
        week = self.context.current_week()
        if week is None:
            return "current week unknown; skipping"
        from ..ingest.scores import sync_scores

        count = sync_scores(self.context.client, self.context.repos, week)
        return f"live scoring week {week}: {count} rows"


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

    def register(name: str, func: Callable[[], str], trigger) -> None:
        def wrapped() -> None:
            # Every outcome is recorded here rather than in each job, so a job
            # added later is watched by the heartbeat without anyone
            # remembering to instrument it.
            try:
                result = func()
                log.info("job %s: %s", name, result)
                record_success(runner.context.repos, name)
            except Exception as exc:  # noqa: BLE001 - one bad job must not kill the loop
                log.exception("job %s failed", name)
                record_failure(runner.context.repos, name, f"{type(exc).__name__}: {exc}")

        scheduler.add_job(wrapped, trigger, id=name, replace_existing=True,
                          max_instances=1, coalesce=True)

    schedule = config.schedule
    register("config_refresh", runner.refresh_config,
             CronTrigger.from_crontab(schedule.config_refresh_cron, timezone="UTC"))
    register("player_db_refresh", runner.refresh_players,
             CronTrigger.from_crontab(schedule.player_db_refresh_cron, timezone="UTC"))
    register("league_state_poll", runner.poll_state,
             IntervalTrigger(minutes=schedule.league_state_poll_minutes))
    register("news_ingest", runner.ingest_news,
             IntervalTrigger(minutes=config.news.poll_minutes))
    register("waiver_analysis", runner.analyse_waivers,
             CronTrigger.from_crontab(schedule.waiver_analysis_cron, timezone="UTC"))
    register("trade_analysis", runner.analyse_trades,
             CronTrigger.from_crontab(schedule.trade_analysis_cron, timezone="UTC"))
    register("expire_recommendations", runner.expire_recommendations,
             IntervalTrigger(minutes=15))
    # The watchdog is registered like any other job, so a stall in the watchdog
    # itself shows up in the same place as any other stalled job.
    register("heartbeat", runner.heartbeat,
             IntervalTrigger(minutes=schedule.heartbeat_minutes))

    # Lineup analysis is scheduled relative to the league's actual deadline,
    # which is only known once the config has been synced. The job re-registers
    # itself each day as the deadline moves week to week.
    def schedule_lineup_jobs() -> str:
        settings = runner.context.league_settings()
        if settings is None or settings.lineup_deadline is None:
            return "lineup deadline unknown; no lineup jobs scheduled"
        times = lineup_run_times(
            settings.lineup_deadline, config.lineup.lead_times_hours
        )
        for index, run_at in enumerate(times):
            scheduler.add_job(
                lambda: log.info("lineup job: %s", runner.analyse_lineup()),
                "date",
                run_date=run_at,
                id=f"lineup_analysis_{index}",
                replace_existing=True,
            )
        return f"scheduled {len(times)} lineup run(s)"

    register("schedule_lineup_jobs", schedule_lineup_jobs,
             CronTrigger.from_crontab("15 5 * * *", timezone="UTC"))

    return scheduler
