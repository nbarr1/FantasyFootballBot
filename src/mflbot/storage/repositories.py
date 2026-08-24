"""Repository layer -- the only place that knows SQL.

Analysis, ingestion and the CLI depend on these methods, never on the database
directly. That indirection is what makes the Postgres swap a matter of writing
a second implementation of this class rather than rewriting the bot.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Iterable, Sequence

from ..domain.models import (
    Franchise,
    LeagueSettings,
    LineupSlot,
    NewsItem,
    Player,
    Projection,
    RosterEntry,
    Transaction,
    WaiverSystem,
)
from ..errors import BlockedFeature
from .db import Database, utc_now_iso


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass(slots=True)
class Repositories:
    """Facade grouping the per-entity repositories."""

    db: Database

    # -- league configuration ---------------------------------------------

    def save_league_settings(self, settings: LeagueSettings, raw: Any) -> None:
        self.db.execute(
            """
            INSERT INTO league_config (
                league_id, season, fetched_at, raw_json, name, franchise_count,
                roster_size, starter_count, taxi_squad_size, injured_reserve,
                waiver_type, trade_deadline, lineup_deadline
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(league_id, season) DO UPDATE SET
                fetched_at=excluded.fetched_at, raw_json=excluded.raw_json,
                name=excluded.name, franchise_count=excluded.franchise_count,
                roster_size=excluded.roster_size, starter_count=excluded.starter_count,
                taxi_squad_size=excluded.taxi_squad_size,
                injured_reserve=excluded.injured_reserve,
                waiver_type=excluded.waiver_type,
                trade_deadline=excluded.trade_deadline,
                lineup_deadline=excluded.lineup_deadline
            """,
            (
                settings.league_id,
                settings.season,
                utc_now_iso(),
                json.dumps(raw, separators=(",", ":")),
                settings.name,
                settings.franchise_count,
                settings.roster_size,
                settings.starter_count,
                settings.taxi_squad_size,
                settings.injured_reserve,
                settings.waiver_type_raw,
                settings.trade_deadline.isoformat() if settings.trade_deadline else None,
                settings.lineup_deadline.isoformat() if settings.lineup_deadline else None,
            ),
        )
        self.db.execute(
            "DELETE FROM lineup_slots WHERE league_id=? AND season=?",
            (settings.league_id, settings.season),
        )
        self.db.executemany(
            "INSERT INTO lineup_slots (league_id, season, slot_index, slot_name, "
            "eligible_pos, min_starters, max_starters) VALUES (?,?,?,?,?,?,?)",
            [
                (
                    settings.league_id,
                    settings.season,
                    slot.index,
                    slot.name,
                    ",".join(slot.eligible_positions),
                    slot.min_starters,
                    slot.max_starters,
                )
                for slot in settings.lineup_slots
            ],
        )
        self.db.executemany(
            """
            INSERT INTO franchises (league_id, season, franchise_id, name, division,
                                    is_owner, bbid_budget, waiver_order)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(league_id, season, franchise_id) DO UPDATE SET
                name=excluded.name, division=excluded.division,
                is_owner=excluded.is_owner, bbid_budget=excluded.bbid_budget,
                waiver_order=excluded.waiver_order
            """,
            [
                (
                    settings.league_id,
                    settings.season,
                    f.franchise_id,
                    f.name,
                    f.division,
                    int(f.is_owner),
                    f.bbid_budget,
                    f.waiver_order,
                )
                for f in settings.franchises
            ],
        )
        self.db.commit()

    def load_league_settings(self, league_id: str, season: int) -> LeagueSettings | None:
        row = self.db.query_one(
            "SELECT * FROM league_config WHERE league_id=? AND season=?",
            (league_id, season),
        )
        if row is None:
            return None
        slots = tuple(
            LineupSlot(
                index=r["slot_index"],
                name=r["slot_name"],
                eligible_positions=tuple(
                    p for p in r["eligible_pos"].split(",") if p
                ),
                min_starters=r["min_starters"],
                max_starters=r["max_starters"],
            )
            for r in self.db.query(
                "SELECT * FROM lineup_slots WHERE league_id=? AND season=? "
                "ORDER BY slot_index",
                (league_id, season),
            )
        )
        franchises = tuple(
            Franchise(
                franchise_id=r["franchise_id"],
                name=r["name"],
                division=r["division"],
                is_owner=bool(r["is_owner"]),
                bbid_budget=r["bbid_budget"],
                waiver_order=r["waiver_order"],
            )
            for r in self.db.query(
                "SELECT * FROM franchises WHERE league_id=? AND season=? "
                "ORDER BY franchise_id",
                (league_id, season),
            )
        )
        from ..ingest.config_sync import normalise_waiver_system  # local: avoids cycle

        return LeagueSettings(
            league_id=row["league_id"],
            season=row["season"],
            name=row["name"],
            franchise_count=row["franchise_count"],
            roster_size=row["roster_size"],
            starter_count=row["starter_count"],
            taxi_squad_size=row["taxi_squad_size"],
            injured_reserve=row["injured_reserve"],
            waiver_type_raw=row["waiver_type"],
            waiver_system=normalise_waiver_system(row["waiver_type"]),
            trade_deadline=_parse_dt(row["trade_deadline"]),
            lineup_deadline=_parse_dt(row["lineup_deadline"]),
            lineup_slots=slots,
            franchises=franchises,
        )

    # -- scoring rules ------------------------------------------------------

    def save_scoring_rules(
        self, league_id: str, season: int, rules: Sequence[Any], gaps: Sequence[Any]
    ) -> None:
        self.db.execute(
            "DELETE FROM scoring_rules WHERE league_id=? AND season=?",
            (league_id, season),
        )
        self.db.execute(
            "DELETE FROM scoring_rule_gaps WHERE league_id=? AND season=?",
            (league_id, season),
        )
        self.db.executemany(
            "INSERT INTO scoring_rules (league_id, season, rule_index, positions, "
            "event_code, points_expr, range_expr, kind, coefficient, range_low, "
            "range_high) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    league_id,
                    season,
                    rule.index,
                    ",".join(rule.positions),
                    rule.event_code,
                    rule.points_expr,
                    rule.range_expr,
                    rule.kind,
                    rule.coefficient,
                    rule.range_low,
                    rule.range_high,
                )
                for rule in rules
            ],
        )
        self.db.executemany(
            "INSERT INTO scoring_rule_gaps (league_id, season, rule_index, positions, "
            "event_code, points_expr, range_expr, reason) VALUES (?,?,?,?,?,?,?,?)",
            [
                (
                    league_id,
                    season,
                    gap.index,
                    ",".join(gap.positions),
                    gap.event_code,
                    gap.points_expr,
                    gap.range_expr,
                    gap.reason,
                )
                for gap in gaps
            ],
        )
        self.db.commit()

    def load_scoring_rule_rows(self, league_id: str, season: int) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.db.query(
                "SELECT * FROM scoring_rules WHERE league_id=? AND season=? "
                "ORDER BY rule_index",
                (league_id, season),
            )
        ]

    def load_scoring_gaps(self, league_id: str, season: int) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.db.query(
                "SELECT * FROM scoring_rule_gaps WHERE league_id=? AND season=? "
                "ORDER BY rule_index",
                (league_id, season),
            )
        ]

    def save_rule_definitions(self, definitions: Iterable[dict[str, Any]]) -> int:
        now = utc_now_iso()
        rows = [
            (
                d["event_code"],
                d.get("short_name"),
                d.get("description"),
                d.get("is_player"),
                d.get("is_team"),
                d.get("is_coach"),
                now,
            )
            for d in definitions
        ]
        self.db.executemany(
            "INSERT INTO rule_definitions (event_code, short_name, description, "
            "is_player, is_team, is_coach, fetched_at) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(event_code) DO UPDATE SET short_name=excluded.short_name, "
            "description=excluded.description, fetched_at=excluded.fetched_at",
            rows,
        )
        self.db.commit()
        return len(rows)

    def known_event_codes(self) -> set[str]:
        return {r["event_code"] for r in self.db.query("SELECT event_code FROM rule_definitions")}

    # -- players ------------------------------------------------------------

    def upsert_players(self, players: Iterable[Player]) -> int:
        now = utc_now_iso()
        rows = [(p.player_id, p.name, p.position, p.nfl_team, p.status, now) for p in players]
        self.db.executemany(
            "INSERT INTO players (player_id, name, position, nfl_team, status, updated_at) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(player_id) DO UPDATE SET "
            "name=excluded.name, position=excluded.position, nfl_team=excluded.nfl_team, "
            "status=excluded.status, updated_at=excluded.updated_at",
            rows,
        )
        self.db.commit()
        return len(rows)

    def get_player(self, player_id: str) -> Player | None:
        row = self.db.query_one("SELECT * FROM players WHERE player_id=?", (player_id,))
        return _row_to_player(row) if row else None

    def get_players(self, player_ids: Sequence[str]) -> dict[str, Player]:
        if not player_ids:
            return {}
        marks = ",".join("?" * len(player_ids))
        rows = self.db.query(
            f"SELECT * FROM players WHERE player_id IN ({marks})",  # noqa: S608
            tuple(player_ids),
        )
        return {r["player_id"]: _row_to_player(r) for r in rows}

    def player_count(self) -> int:
        return self.db.query_one("SELECT COUNT(*) AS n FROM players")["n"]

    # -- rosters and free agents -------------------------------------------

    def save_roster_snapshot(
        self, league_id: str, season: int, entries: Iterable[RosterEntry]
    ) -> str:
        snapshot_at = utc_now_iso()
        self.db.executemany(
            "INSERT OR REPLACE INTO rosters (league_id, season, franchise_id, player_id, "
            "roster_status, snapshot_at) VALUES (?,?,?,?,?,?)",
            [
                (league_id, season, e.franchise_id, e.player_id, e.roster_status, snapshot_at)
                for e in entries
            ],
        )
        self.db.commit()
        return snapshot_at

    def latest_roster_snapshot_at(self, league_id: str, season: int) -> str | None:
        row = self.db.query_one(
            "SELECT MAX(snapshot_at) AS s FROM rosters WHERE league_id=? AND season=?",
            (league_id, season),
        )
        return row["s"] if row else None

    def current_rosters(self, league_id: str, season: int) -> dict[str, list[RosterEntry]]:
        snapshot = self.latest_roster_snapshot_at(league_id, season)
        if snapshot is None:
            return {}
        out: dict[str, list[RosterEntry]] = {}
        for row in self.db.query(
            "SELECT * FROM rosters WHERE league_id=? AND season=? AND snapshot_at=?",
            (league_id, season, snapshot),
        ):
            out.setdefault(row["franchise_id"], []).append(
                RosterEntry(row["franchise_id"], row["player_id"], row["roster_status"])
            )
        return out

    def save_free_agents(
        self, league_id: str, season: int, player_ids: Iterable[str]
    ) -> str:
        snapshot_at = utc_now_iso()
        self.db.executemany(
            "INSERT OR REPLACE INTO free_agents (league_id, season, player_id, snapshot_at) "
            "VALUES (?,?,?,?)",
            [(league_id, season, pid, snapshot_at) for pid in player_ids],
        )
        self.db.commit()
        return snapshot_at

    def current_free_agents(self, league_id: str, season: int) -> list[str]:
        row = self.db.query_one(
            "SELECT MAX(snapshot_at) AS s FROM free_agents WHERE league_id=? AND season=?",
            (league_id, season),
        )
        if not row or not row["s"]:
            return []
        return [
            r["player_id"]
            for r in self.db.query(
                "SELECT player_id FROM free_agents WHERE league_id=? AND season=? "
                "AND snapshot_at=?",
                (league_id, season, row["s"]),
            )
        ]

    # -- scores and projections ---------------------------------------------

    def save_scores(
        self, league_id: str, season: int, week: int, scores: Iterable[tuple[str, float]],
        *, is_final: bool = False,
    ) -> int:
        now = utc_now_iso()
        rows = [
            (league_id, season, pid, week, points, int(is_final), now)
            for pid, points in scores
        ]
        self.db.executemany(
            "INSERT INTO scores (league_id, season, player_id, week, points, is_final, "
            "fetched_at) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(league_id, season, player_id, week) DO UPDATE SET "
            "points=excluded.points, is_final=excluded.is_final, "
            "fetched_at=excluded.fetched_at",
            rows,
        )
        self.db.commit()
        return len(rows)

    def load_scores(self, league_id: str, season: int, week: int) -> dict[str, float]:
        return {
            r["player_id"]: r["points"]
            for r in self.db.query(
                "SELECT player_id, points FROM scores WHERE league_id=? AND season=? "
                "AND week=?",
                (league_id, season, week),
            )
        }

    def save_projections(
        self, league_id: str, season: int, projections: Iterable[Projection]
    ) -> int:
        now = utc_now_iso()
        rows = [
            (league_id, season, p.player_id, p.week, p.source, p.points, now)
            for p in projections
        ]
        self.db.executemany(
            "INSERT INTO projections (league_id, season, player_id, week, source, points, "
            "fetched_at) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(league_id, season, player_id, week, source) DO UPDATE SET "
            "points=excluded.points, fetched_at=excluded.fetched_at",
            rows,
        )
        self.db.commit()
        return len(rows)

    def load_projections(
        self, league_id: str, season: int, week: int, source: str | None = None
    ) -> dict[str, float]:
        sql = (
            "SELECT player_id, points FROM projections WHERE league_id=? AND season=? "
            "AND week=?"
        )
        params: tuple[Any, ...] = (league_id, season, week)
        if source:
            sql += " AND source=?"
            params += (source,)
        return {r["player_id"]: r["points"] for r in self.db.query(sql, params)}

    def projection_weeks(self, league_id: str, season: int) -> list[int]:
        return [
            r["week"]
            for r in self.db.query(
                "SELECT DISTINCT week FROM projections WHERE league_id=? AND season=? "
                "ORDER BY week",
                (league_id, season),
            )
        ]

    # -- transactions -------------------------------------------------------

    def save_transactions(
        self, league_id: str, season: int, transactions: Iterable[Transaction]
    ) -> list[Transaction]:
        """Insert transactions, returning only those not seen before.

        The returned list is the change-detection feed that triggers analysis.
        """
        now = utc_now_iso()
        new: list[Transaction] = []
        for tx in transactions:
            existing = self.db.query_one(
                "SELECT 1 FROM transactions WHERE league_id=? AND season=? "
                "AND transaction_id=?",
                (league_id, season, tx.transaction_id),
            )
            if existing:
                continue
            self.db.execute(
                "INSERT INTO transactions (league_id, season, transaction_id, timestamp, "
                "trans_type, franchise_id, raw_json, seen_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    league_id,
                    season,
                    tx.transaction_id,
                    tx.timestamp.isoformat(),
                    tx.trans_type,
                    tx.franchise_id,
                    json.dumps(tx.raw, separators=(",", ":")),
                    now,
                ),
            )
            new.append(tx)
        self.db.commit()
        return new

    def transaction_count(self, league_id: str, season: int) -> int:
        return self.db.query_one(
            "SELECT COUNT(*) AS n FROM transactions WHERE league_id=? AND season=?",
            (league_id, season),
        )["n"]

    # -- news ---------------------------------------------------------------

    def save_news(self, items: Iterable[NewsItem]) -> list[NewsItem]:
        now = utc_now_iso()
        new: list[NewsItem] = []
        for item in items:
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO news_items (source, external_id, player_id, "
                "player_name, published_at, ingested_at, classification, headline, body, url) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    item.source,
                    item.external_id,
                    item.player_id,
                    item.player_name,
                    item.published_at.isoformat() if item.published_at else None,
                    now,
                    item.classification,
                    item.headline,
                    item.body,
                    item.url,
                ),
            )
            if cursor.rowcount:
                new.append(item)
        self.db.commit()
        return new

    def recent_news_for_players(
        self, player_ids: Sequence[str], limit: int = 5
    ) -> dict[str, list[dict[str, Any]]]:
        if not player_ids:
            return {}
        marks = ",".join("?" * len(player_ids))
        rows = self.db.query(
            f"SELECT * FROM news_items WHERE player_id IN ({marks}) "  # noqa: S608
            "ORDER BY COALESCE(published_at, ingested_at) DESC",
            tuple(player_ids),
        )
        out: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            bucket = out.setdefault(row["player_id"], [])
            if len(bucket) < limit:
                bucket.append(dict(row))
        return out

    # -- blocked features ---------------------------------------------------

    def block_feature(self, blocked: BlockedFeature) -> None:
        self.db.execute(
            "INSERT INTO blocked_features (feature, reason, gaps_json, remedy, blocked_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT(feature) DO UPDATE SET reason=excluded.reason, "
            "gaps_json=excluded.gaps_json, remedy=excluded.remedy, "
            "blocked_at=excluded.blocked_at",
            (
                blocked.feature,
                blocked.reason,
                json.dumps(list(blocked.gaps)),
                blocked.remedy,
                utc_now_iso(),
            ),
        )
        self.db.commit()

    def unblock_feature(self, feature: str) -> None:
        self.db.execute("DELETE FROM blocked_features WHERE feature=?", (feature,))
        self.db.commit()

    def blocked_features(self) -> list[BlockedFeature]:
        return [
            BlockedFeature(
                feature=r["feature"],
                reason=r["reason"],
                gaps=tuple(json.loads(r["gaps_json"])),
                remedy=r["remedy"],
            )
            for r in self.db.query("SELECT * FROM blocked_features ORDER BY feature")
        ]

    # -- ingest bookkeeping -------------------------------------------------

    def set_state(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO ingest_state (key, value, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at",
            (key, value, utc_now_iso()),
        )
        self.db.commit()

    def get_state(self, key: str) -> str | None:
        row = self.db.query_one("SELECT value FROM ingest_state WHERE key=?", (key,))
        return row["value"] if row else None

    # -- audit --------------------------------------------------------------

    def audit(
        self,
        *,
        request_summary: str,
        outcome: str,
        recommendation_id: str | None = None,
        token_id: str | None = None,
        capability: str | None = None,
        endpoint_type: str | None = None,
        response_summary: str | None = None,
        confirmed: bool = False,
        detail: dict[str, Any] | None = None,
    ) -> int:
        from ..mfl.auth import redact  # local import keeps storage import-light

        cursor = self.db.execute(
            "INSERT INTO audit_log (at, recommendation_id, token_id, capability, "
            "endpoint_type, request_summary, outcome, response_summary, confirmed, "
            "detail_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                utc_now_iso(),
                recommendation_id,
                token_id,
                capability,
                endpoint_type,
                redact(request_summary),
                outcome,
                redact(response_summary) if response_summary else None,
                int(confirmed),
                json.dumps(detail or {}, separators=(",", ":")),
            ),
        )
        self.db.commit()
        return cursor.lastrowid or 0

    def audit_entries(self, limit: int = 50) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.db.query(
                "SELECT * FROM audit_log ORDER BY at DESC, id DESC LIMIT ?", (limit,)
            )
        ]


def _row_to_player(row) -> Player:
    return Player(
        player_id=row["player_id"],
        name=row["name"],
        position=row["position"],
        nfl_team=row["nfl_team"],
        status=row["status"],
    )
