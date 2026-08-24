"""Wiring: one object that owns the bot's components and the analysis runs.

Kept separate from the CLI so that the scheduler, the web dashboard and the
tests all construct the same graph.

Note what this object does *not* hold by default: a write client. It is created
on demand, only inside :meth:`execute_approved`, which requires a token. The
analysis methods here cannot reach it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .analysis.lineup import Candidate, diff_lineup, optimise_lineup
from .analysis.projections import ProjectionProvider
from .analysis.rules_parser import ParsedRules, ScoringRule, ScoringRuleGap
from .analysis.valuation import ScoringModel
from .approval.cli_channel import CLIApprovalChannel
from .approval.token import TokenService
from .config import Config
from .domain.models import LeagueSettings, Player
from .errors import Missing
from .mfl.client import MFLReadClient
from .mfl.endpoints import EndpointRegistry
from .notify.registry import build_notifier
from .recommend.models import (
    Confidence,
    Evidence,
    LineupPayload,
    Recommendation,
    RecommendationKind,
)
from .recommend.store import RecommendationStore
from .storage.db import Database
from .storage.repositories import Repositories

log = logging.getLogger(__name__)


@dataclass(slots=True)
class BotContext:
    config: Config
    db: Database
    repos: Repositories
    store: RecommendationStore
    tokens: TokenService
    client: MFLReadClient
    registry: EndpointRegistry
    notifier: Any
    channel: CLIApprovalChannel

    @classmethod
    def build(cls, config: Config) -> BotContext:
        db = Database.from_settings(config.storage)
        db.migrate()
        repos = Repositories(db)
        store = RecommendationStore(db)
        tokens = TokenService.create(db)
        registry = EndpointRegistry.load()
        client = MFLReadClient(config.league, registry=registry)
        notifier = build_notifier(config.notify)
        channel = CLIApprovalChannel(store, tokens, notifier)
        return cls(config, db, repos, store, tokens, client, registry, notifier, channel)

    def close(self) -> None:
        self.client.close()
        self.db.close()

    # -- shared lookups ----------------------------------------------------

    def league_settings(self) -> LeagueSettings | None:
        return self.repos.load_league_settings(
            self.config.league.id, self.config.league.season
        )

    def scoring_model(self) -> ScoringModel:
        """Rebuild the scoring model from what was parsed and stored."""
        league_id, season = self.config.league.id, self.config.league.season
        rules = tuple(
            ScoringRule(
                index=r["rule_index"],
                positions=tuple(p for p in r["positions"].split(",") if p),
                event_code=r["event_code"],
                points_expr=r["points_expr"],
                range_expr=r["range_expr"],
                kind=r["kind"],
                coefficient=r["coefficient"],
                range_low=r["range_low"],
                range_high=r["range_high"],
            )
            for r in self.repos.load_scoring_rule_rows(league_id, season)
        )
        gaps = tuple(
            ScoringRuleGap(
                index=g["rule_index"],
                positions=tuple(p for p in (g["positions"] or "").split(",") if p),
                event_code=g["event_code"],
                points_expr=g["points_expr"],
                range_expr=g["range_expr"],
                reason=g["reason"],
            )
            for g in self.repos.load_scoring_gaps(league_id, season)
        )
        return ScoringModel(
            parsed=ParsedRules(rules, gaps),
            known_events=frozenset(self.repos.known_event_codes()),
        )

    def projections(self) -> ProjectionProvider:
        return ProjectionProvider(
            self.repos, self.config.league.id, self.config.league.season
        )

    def owner_franchise_id(self) -> str | None:
        configured = self.config.league.franchise_id
        if configured:
            return configured
        settings = self.league_settings()
        owner = settings.owner_franchise if settings else None
        return owner.franchise_id if owner else None

    def current_week(self) -> int | None:
        """The current NFL week, read from MFL rather than computed from a date."""
        cached = self.repos.get_state("current_week")
        if cached:
            try:
                return int(cached)
            except ValueError:
                pass
        try:
            payload = self.client.export("nflSchedule").payload
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not determine the current week: %s", exc)
            return None
        root = payload.get("nflSchedule") if isinstance(payload, dict) else None
        if not isinstance(root, dict):
            return None
        from .analysis.rules_parser import mfl_text

        week = mfl_text(root.get("week"))
        if week and week.isdigit():
            self.repos.set_state("current_week", week)
            return int(week)
        return None

    def remaining_weeks(self, from_week: int) -> list[int]:
        """Weeks with projections available from ``from_week`` onward.

        Derived from stored data, not from an assumed 17- or 18-week season.
        """
        weeks = self.repos.projection_weeks(
            self.config.league.id, self.config.league.season
        )
        return [w for w in weeks if w >= from_week]

    def roster_players(self, franchise_id: str) -> list[Player]:
        rosters = self.repos.current_rosters(
            self.config.league.id, self.config.league.season
        )
        entries = [e for e in rosters.get(franchise_id, []) if e.is_active_roster]
        lookup = self.repos.get_players([e.player_id for e in entries])
        return [lookup[e.player_id] for e in entries if e.player_id in lookup]

    def free_agent_players(self) -> list[Player]:
        ids = self.repos.current_free_agents(
            self.config.league.id, self.config.league.season
        )
        lookup = self.repos.get_players(ids)
        return [lookup[pid] for pid in ids if pid in lookup]

    # -- NFL context -------------------------------------------------------

    def week_schedule(self, week: int) -> tuple[set[str], dict[str, datetime]]:
        """Teams playing this week and each team's kickoff time.

        Bye weeks are derived from the real schedule (a team absent from the
        week's games is on bye), never from a hardcoded bye table.
        """
        from .analysis.rules_parser import mfl_text

        try:
            payload = self.client.export("nflSchedule", W=week).payload
        except Exception as exc:  # noqa: BLE001
            log.warning("NFL schedule unavailable for week %s: %s", week, exc)
            return set(), {}
        root = payload.get("nflSchedule") if isinstance(payload, dict) else None
        if not isinstance(root, dict):
            return set(), {}
        games = root.get("matchup", [])
        if isinstance(games, dict):
            games = [games]

        playing: set[str] = set()
        kickoffs: dict[str, datetime] = {}
        for game in games or []:
            if not isinstance(game, dict):
                continue
            kickoff_raw = mfl_text(game.get("kickoff"))
            kickoff = None
            if kickoff_raw and kickoff_raw.isdigit():
                try:
                    kickoff = datetime.fromtimestamp(int(kickoff_raw), tz=UTC)
                except (OverflowError, OSError, ValueError):
                    kickoff = None
            teams = game.get("team", [])
            if isinstance(teams, dict):
                teams = [teams]
            for team in teams or []:
                if not isinstance(team, dict):
                    continue
                team_id = mfl_text(team.get("id"))
                if not team_id:
                    continue
                playing.add(team_id)
                if kickoff is not None:
                    kickoffs[team_id] = kickoff
        return playing, kickoffs

    def injury_statuses(self, week: int | None = None) -> dict[str, str]:
        from .analysis.rules_parser import mfl_text

        try:
            payload = self.client.export("injuries", W=week).payload
        except Exception as exc:  # noqa: BLE001
            log.warning("Injury feed unavailable: %s", exc)
            return {}
        root = payload.get("injuries") if isinstance(payload, dict) else None
        if not isinstance(root, dict):
            return {}
        nodes = root.get("injury", [])
        if isinstance(nodes, dict):
            nodes = [nodes]
        out: dict[str, str] = {}
        for node in nodes or []:
            if not isinstance(node, dict):
                continue
            pid = mfl_text(node.get("id"))
            status = mfl_text(node.get("status"))
            if pid and status:
                out[pid] = status
        return out

    # -- analysis runs -----------------------------------------------------

    def _blocked(self, feature: str) -> str | None:
        for blocked in self.repos.blocked_features():
            if blocked.feature == feature:
                return blocked.describe()
        return None

    def run_lineup_analysis(self, week: int | None = None) -> str:
        blocked = self._blocked("lineup")
        if blocked:
            return blocked

        settings = self.league_settings()
        if settings is None:
            return "League settings not synced. Run `bot sync-config` first."
        franchise_id = self.owner_franchise_id()
        if franchise_id is None:
            return (
                "Your own franchise id is not known, so there is no lineup to set. "
                "Run `bot whoami`, or set league.franchise_id in config.toml."
            )
        week = week or self.current_week()
        if week is None:
            return "The current NFL week could not be determined."

        roster = self.roster_players(franchise_id)
        if not roster:
            return "No roster is stored yet. Run `bot poll` first."

        projections = self.projections()
        week_projections = projections.week(week)
        playing, kickoffs = self.week_schedule(week)
        injuries = self.injury_statuses(week)

        candidates: list[Candidate] = []
        unprojected: list[str] = []
        for player in roster:
            points = week_projections.get(player.player_id)
            if points is None:
                unprojected.append(player.display)
                continue
            candidates.append(
                Candidate(
                    player=player,
                    projection=points,
                    kickoff=kickoffs.get(player.nfl_team or ""),
                    injury_status=injuries.get(player.player_id),
                    on_bye=bool(playing) and (player.nfl_team or "") not in playing,
                )
            )

        if not candidates:
            return (
                f"No rostered player has a projection for week {week}, so no lineup "
                f"can be recommended. Run `bot sync-projections --week {week}`; if "
                f"MFL publishes none for this league, lineup analysis stays blocked."
            )

        solution = optimise_lineup(
            settings.lineup_slots,
            candidates,
            lock_time=settings.lineup_deadline,
            escalate_within_hours=self.config.lineup.escalate_within_hours,
        )
        if isinstance(solution, Missing):
            return f"Lineup analysis blocked: {solution}"
        solution.unprojected = unprojected

        submitted = self._submitted_starters(franchise_id, week)
        lookup = {p.player_id: p for p in roster}
        changes = diff_lineup(solution, submitted, lookup) if submitted else []

        if submitted and not changes:
            return (
                f"Week {week}: the submitted lineup already matches the highest "
                f"projected legal lineup ({solution.total_projection:.1f} pts). "
                f"No change recommended."
            )

        caveats = list(solution.caveats)
        if unprojected:
            caveats.append(
                f"{len(unprojected)} rostered player(s) had no projection and were "
                f"not considered: {', '.join(unprojected[:5])}"
                + ("..." if len(unprojected) > 5 else "")
            )
        if not submitted:
            caveats.append(
                "The currently submitted lineup could not be read, so this is the "
                "optimal lineup rather than a diff against what you have set."
            )
        coverage = projections.coverage([c.player_id for c in candidates], week)
        if coverage < 0.8:
            caveats.append(
                f"Only {coverage:.0%} of considered players have projections."
            )
        if solution.solver == "greedy":
            caveats.append(
                "Solved greedily (exact for this league's slot structure). Install "
                "the 'solver' extra for ILP if the structure changes."
            )

        rationale_lines = [
            f"Week {week} lineup, {solution.total_projection:.1f} projected points.",
        ]
        if changes:
            rationale_lines.append(f"{len(changes)} change(s) from what is submitted:")
            for change in changes:
                out_name = change.player_out.display if change.player_out else "(empty)"
                rationale_lines.append(
                    f"  {change.slot.name}: {out_name} -> "
                    f"{change.player_in.player.display} ({change.reason})"
                )
        risks = [r for a in solution.assignments for r in a.risks]
        if risks:
            rationale_lines.append("Risk flags on recommended starters:")
            rationale_lines.extend(f"  - {r}" for r in risks)
        rationale_lines.append(
            "Projections are estimates. Late-breaking inactives can invalidate this."
        )

        recommendation = Recommendation(
            kind=RecommendationKind.LINEUP,
            payload=LineupPayload(
                capability=__import__(
                    "mflbot.mfl.endpoints", fromlist=["Capability"]
                ).Capability.SUBMIT_LINEUP,
                league_id=self.config.league.id,
                franchise_id=franchise_id,
                week=week,
                starter_ids=tuple(a.candidate.player_id for a in solution.assignments),
                slot_names=tuple(a.slot.name for a in solution.assignments),
            ),
            rationale="\n".join(rationale_lines),
            evidence=Evidence(
                projections={
                    a.candidate.player_id: a.candidate.projection
                    for a in solution.assignments
                },
                sources=("mfl_projectedScores", "mfl_injuries", "mfl_nflSchedule"),
                notes={"solver": solution.solver, "coverage": round(coverage, 3)},
            ),
            confidence=Confidence.HIGH if coverage >= 0.9 and not risks
            else Confidence.MEDIUM if coverage >= 0.6 else Confidence.LOW,
            caveats=tuple(caveats),
            expires_at=settings.lineup_deadline
            or (datetime.now(UTC) + timedelta(days=1)),
        )
        self.store.save(recommendation)

        urgent = bool(solution.urgent_risks())
        self.notifier.send(
            f"Week {week} lineup: {len(changes) or 'no'} change(s) recommended",
            recommendation.render(),
            urgent=urgent,
        )
        return f"Created lineup recommendation {recommendation.id}"

    def _submitted_starters(self, franchise_id: str, week: int) -> list[str]:
        """Read the lineup currently submitted, if the endpoint exposes it.

        Returns an empty list when it cannot be read, which the caller reports
        as a caveat rather than treating as "nothing is set".
        """
        try:
            payload = self.client.export(
                "weeklyResults", L=self.config.league.id, W=week
            ).payload
        except Exception as exc:  # noqa: BLE001
            log.info("Could not read the submitted lineup: %s", exc)
            return []
        from .analysis.rules_parser import mfl_text

        def walk(node: Any) -> list[str]:
            found: list[str] = []
            if isinstance(node, dict):
                if mfl_text(node.get("id")) and mfl_text(node.get("status")) == "starter":
                    pid = mfl_text(node.get("id"))
                    if pid:
                        found.append(pid)
                for value in node.values():
                    found.extend(walk(value))
            elif isinstance(node, list):
                for value in node:
                    found.extend(walk(value))
            return found

        franchise_blob = payload
        return walk(franchise_blob)

    def run_waiver_analysis(self, week: int | None = None) -> str:
        blocked = self._blocked("waivers")
        if blocked:
            return blocked
        settings = self.league_settings()
        if settings is None:
            return "League settings not synced. Run `bot sync-config` first."
        franchise_id = self.owner_franchise_id()
        if franchise_id is None:
            return "Your own franchise id is not known. Run `bot whoami`."
        week = week or self.current_week()
        if week is None:
            return "The current NFL week could not be determined."

        from .analysis.waivers import analyse_waivers, build_recommendations

        roster = self.roster_players(franchise_id)
        free_agents = self.free_agent_players()
        if not roster or not free_agents:
            return "Roster or free-agent pool is not stored yet. Run `bot poll` first."

        projections = self.projections()
        remaining = self.remaining_weeks(week)
        news = self.repos.recent_news_for_players([p.player_id for p in free_agents])

        ideas, blocks = analyse_waivers(
            settings, roster, free_agents, projections, week, remaining,
            self.config.waivers, news_by_player=news,
        )
        for block in blocks:
            self.repos.block_feature(block)
        if blocks:
            return "\n".join(b.describe() for b in blocks)
        if not ideas:
            return "No add/drop meets your thresholds right now."

        recommendations = build_recommendations(ideas, settings, franchise_id)
        for recommendation in recommendations:
            self.store.save(recommendation)
        self.notifier.send(
            f"{len(recommendations)} add/drop idea(s)",
            "\n\n".join(r.render() for r in recommendations),
        )
        return f"Created {len(recommendations)} add/drop recommendation(s): " + ", ".join(
            r.id for r in recommendations
        )

    def run_trade_analysis(self, week: int | None = None) -> str:
        blocked = self._blocked("trades")
        if blocked:
            return blocked
        settings = self.league_settings()
        if settings is None:
            return "League settings not synced. Run `bot sync-config` first."
        franchise_id = self.owner_franchise_id()
        if franchise_id is None:
            return "Your own franchise id is not known. Run `bot whoami`."
        week = week or self.current_week()
        if week is None:
            return "The current NFL week could not be determined."

        from .analysis.trades import build_proposal_recommendations, draft_proposals
        from .analysis.waivers import value_players

        projections = self.projections()
        remaining = self.remaining_weeks(week)
        rosters = self.repos.current_rosters(
            self.config.league.id, self.config.league.season
        )
        if not rosters:
            return "No rosters stored yet. Run `bot poll` first."

        our_values = value_players(
            self.roster_players(franchise_id), projections, week, remaining
        )
        theirs: dict[str, list] = {}
        for other_id in rosters:
            if other_id == franchise_id:
                continue
            theirs[other_id] = value_players(
                self.roster_players(other_id), projections, week, remaining
            )

        ideas, blocks = draft_proposals(
            settings, our_values, theirs, self.config.trades
        )
        for block in blocks:
            self.repos.block_feature(block)
        if blocks:
            return "\n".join(b.describe() for b in blocks)
        if not ideas:
            return "No trade proposal clears the mutual-gain threshold this week."

        recommendations = build_proposal_recommendations(ideas, settings, franchise_id)
        for recommendation in recommendations:
            self.store.save(recommendation)
        self.notifier.send(
            f"{len(recommendations)} trade idea(s)",
            "\n\n".join(r.render() for r in recommendations),
        )
        return f"Created {len(recommendations)} trade proposal(s): " + ", ".join(
            r.id for r in recommendations
        )

    # -- execution ---------------------------------------------------------

    def execute_approved(self, recommendation: Recommendation, token) -> Any:
        """Build the write client and execute. The only path to an MFL write."""
        from .execute.executor import Executor
        from .mfl.write_client import MFLWriteClient

        self.client.ensure_authenticated("executing an approved action")
        with MFLWriteClient(
            self.config.league,
            self.client.auth,
            self.tokens,
            registry=self.registry,
            rate_limiter=self.client.rate_limiter,
        ) as write_client:
            executor = Executor(write_client, self.client, self.repos, self.store)
            return executor.execute(recommendation, token)
