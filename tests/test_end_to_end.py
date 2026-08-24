"""End-to-end: config sync -> ingestion -> analysis -> approval -> execution.

Drives the real :class:`~mflbot.context.BotContext` against a fake MFL server.
Every payload the fake serves is SYNTHETIC, in the response shapes MFL is
documented to use, so the production parsers, analysis engines, approval flow
and executor all run for real.

The final assertion is the one that matters most: walking the entire pipeline
without an explicit approval submits nothing.
"""

from __future__ import annotations

import json

import pytest

from mflbot.approval.cli_channel import CLIApprovalChannel
from mflbot.approval.token import TokenService
from mflbot.config import (
    Config,
    LeagueRef,
    LineupSettings,
    NotifySettings,
    StorageSettings,
    TradeSettings,
    WaiverSettings,
)
from mflbot.context import BotContext
from mflbot.mfl.cache import ResponseCache
from mflbot.mfl.client import MFLReadClient
from mflbot.mfl.endpoints import EndpointRegistry
from mflbot.mfl.ratelimit import RateLimiter, RateLimitPolicy
from mflbot.notify.base import NullNotifier
from mflbot.recommend.models import RecommendationKind, RecommendationStatus
from mflbot.recommend.store import RecommendationStore
from mflbot.storage.db import Database
from mflbot.storage.repositories import Repositories

LEAGUE_ID = "TEST0001"
SEASON = 2026

# --- SYNTHETIC MFL responses ------------------------------------------------
# Shapes mirror MFL's documented envelopes; every value is invented for testing.

SYNTHETIC_PLAYERS = [
    ("p-qb1", "Synthetic QB One", "QB", "AAA"),
    ("p-qb2", "Synthetic QB Two", "QB", "BBB"),
    ("p-rb1", "Synthetic RB One", "RB", "AAA"),
    ("p-rb2", "Synthetic RB Two", "RB", "BBB"),
    ("p-rb3", "Synthetic RB Three", "RB", "CCC"),
    ("p-wr1", "Synthetic WR One", "WR", "AAA"),
    ("p-wr2", "Synthetic WR Two", "WR", "BBB"),
    ("p-te1", "Synthetic TE One", "TE", "AAA"),
    ("p-fa-rb", "Synthetic FA RB", "RB", "DDD"),
]

OUR_ROSTER = ["p-qb1", "p-rb1", "p-rb2", "p-wr1", "p-wr2", "p-te1"]
THEIR_ROSTER = ["p-qb2", "p-rb3"]
FREE_AGENTS = ["p-fa-rb"]

WEEK = 5
SYNTHETIC_PROJECTIONS = {
    "p-qb1": 21.0, "p-rb1": 17.0, "p-rb2": 4.0, "p-wr1": 15.0,
    "p-wr2": 12.0, "p-te1": 9.0, "p-fa-rb": 14.0, "p-qb2": 8.0, "p-rb3": 6.0,
}


def _t(value):
    return {"$t": str(value)}


PAYLOADS = {
    "league": {
        "league": {
            "id": LEAGUE_ID,
            "name": "Synthetic Integration League",
            "waiverType": "FCFS",
            "rosterSize": "16",
            "tradeDeadline": "4102444800",  # far future
            "lineupDeadline": "4102444800",
            "starters": {
                "position": [
                    {"name": "QB", "limit": "1"},
                    {"name": "RB", "limit": "2"},
                    {"name": "WR", "limit": "2"},
                    {"name": "TE", "limit": "1"},
                ]
            },
            "franchises": {
                "count": "2",
                "franchise": [
                    {"id": "0001", "name": "Ours"},
                    {"id": "0002", "name": "Theirs"},
                ],
            },
        }
    },
    "rules": {
        "rules": {
            "positionRules": [
                {
                    "positions": _t("QB,RB,WR,TE"),
                    "rule": [
                        {"event": _t("XYD"), "points": _t("*.1"), "range": _t("0-9999")},
                        {"event": _t("XTD"), "points": _t("*6")},
                    ],
                }
            ]
        }
    },
    "allRules": {
        "allRules": {
            "rule": [
                {"abbreviation": _t("XYD"), "shortDescription": _t("Synthetic yards")},
                {"abbreviation": _t("XTD"), "shortDescription": _t("Synthetic TDs")},
            ]
        }
    },
    "players": {
        "players": {
            "player": [
                {"id": pid, "name": name, "position": pos, "team": team}
                for pid, name, pos, team in SYNTHETIC_PLAYERS
            ]
        }
    },
    "rosters": {
        "rosters": {
            "franchise": [
                {"id": "0001",
                 "player": [{"id": p, "status": "ROSTER"} for p in OUR_ROSTER]},
                {"id": "0002",
                 "player": [{"id": p, "status": "ROSTER"} for p in THEIR_ROSTER]},
            ]
        }
    },
    "freeAgents": {
        "freeAgents": {"leagueUnit": {"player": [{"id": p} for p in FREE_AGENTS]}}
    },
    "transactions": {
        "transactions": {
            "transaction": [
                {"timestamp": "1700000000", "type": "FREE_AGENT",
                 "franchise": "0002", "transaction": "p-rb3|"}
            ]
        }
    },
    "projectedScores": {
        "projectedScores": {
            "playerScore": [
                {"id": pid, "score": str(points)}
                for pid, points in SYNTHETIC_PROJECTIONS.items()
            ]
        }
    },
    "nflSchedule": {
        "nflSchedule": {
            "week": str(WEEK),
            "matchup": [
                {"kickoff": "1700000000",
                 "team": [{"id": "AAA"}, {"id": "BBB"}]},
                {"kickoff": "1700000000",
                 "team": [{"id": "CCC"}, {"id": "DDD"}]},
            ],
        }
    },
    "injuries": {"injuries": {"week": str(WEEK), "injury": []}},
    "weeklyResults": {"weeklyResults": {}},
}


class FakeMFLResponse:
    def __init__(self, payload) -> None:
        self._payload = payload
        self.status_code = 200
        self.headers = {}
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class FakeMFLTransport:
    """Serves the synthetic payloads and records every request."""

    def __init__(self) -> None:
        self.requests: list[dict] = []

    def get(self, url, params=None, headers=None):
        params = dict(params or {})
        self.requests.append({"url": url, **params})
        return FakeMFLResponse(PAYLOADS.get(params.get("TYPE"), {}))

    def post(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError(
            "the read path must never issue a POST to MFL"
        )

    def close(self):
        return None


@pytest.fixture
def context(tmp_path) -> BotContext:
    config = Config(
        league=LeagueRef(LEAGUE_ID, SEASON, "example.invalid", franchise_id="0001"),
        storage=StorageSettings(path=str(tmp_path / "test.db")),
        waivers=WaiverSettings(),
        trades=TradeSettings(),
        lineup=LineupSettings(),
        notify=NotifySettings(enabled=False),
    )
    db = Database(":memory:")
    db.migrate()
    transport = FakeMFLTransport()
    client = MFLReadClient(
        config.league,
        registry=EndpointRegistry.load("does-not-exist.json"),
        cache=ResponseCache(tmp_path / "cache"),
        rate_limiter=RateLimiter(RateLimitPolicy(min_interval_seconds=0)),
        transport=transport,
    )
    # A session cookie, as a successful login would produce.
    client.auth.session_cookie = "synthetic-session"

    store = RecommendationStore(db)
    tokens = TokenService(db=db, secret=b"integration-test-key")
    ctx = BotContext(
        config=config,
        db=db,
        repos=Repositories(db),
        store=store,
        tokens=tokens,
        client=client,
        registry=client.registry,
        notifier=NullNotifier("disabled in tests"),
        channel=CLIApprovalChannel(store, tokens, writer=lambda _: None),
    )
    yield ctx
    db.close()


def ingest_everything(context: BotContext) -> None:
    from mflbot.ingest.config_sync import sync_config
    from mflbot.ingest.league_state import poll_league_state
    from mflbot.ingest.players import sync_players
    from mflbot.ingest.scores import sync_projections

    sync_config(context.client, context.repos, owner_franchise_id="0001")
    sync_players(context.client, context.repos)
    poll_league_state(context.client, context.repos)
    sync_projections(context.client, context.repos, WEEK)


def test_full_ingestion_populates_the_store_from_the_api(context) -> None:
    assert context.db.table_counts()["players"] == 0, "must start empty"

    ingest_everything(context)

    counts = context.db.table_counts()
    assert counts["players"] == len(SYNTHETIC_PLAYERS)
    assert counts["scoring_rules"] == 2
    assert counts["scoring_rule_gaps"] == 0
    assert counts["lineup_slots"] == 4
    assert counts["projections"] == len(SYNTHETIC_PROJECTIONS)
    assert counts["transactions"] == 1


def test_parsed_config_matches_what_the_api_reported(context) -> None:
    ingest_everything(context)
    settings = context.league_settings()
    assert settings.name == "Synthetic Integration League"
    assert settings.waiver_system == "fcfs"
    assert settings.roster_size == 16
    assert settings.trade_deadline is not None
    assert [s.name for s in settings.lineup_slots] == ["QB", "RB", "WR", "TE"]
    assert settings.owner_franchise.franchise_id == "0001"
    assert context.repos.blocked_features() == []


def test_lineup_analysis_produces_an_approvable_recommendation(context) -> None:
    ingest_everything(context)

    result = context.run_lineup_analysis(week=WEEK)
    assert "Created lineup recommendation" in result

    pending = context.store.pending()
    assert len(pending) == 1
    recommendation = pending[0]
    assert recommendation.kind == RecommendationKind.LINEUP

    payload = recommendation.payload
    assert payload.week == WEEK
    assert payload.franchise_id == "0001"
    # Best legal lineup: QB1, RB1+RB2 (only two RBs rostered), WR1+WR2, TE1.
    assert set(payload.starter_ids) == set(OUR_ROSTER)
    assert "Projections are estimates" in recommendation.rationale
    # The literal payload is shown, not just a summary.
    assert "starter_ids" in recommendation.render()


def test_waiver_analysis_finds_the_upgrade_over_the_weakest_starter(context) -> None:
    ingest_everything(context)

    result = context.run_waiver_analysis(week=WEEK)
    assert "Created" in result, result

    add_drops = [
        r for r in context.store.pending() if r.kind == RecommendationKind.ADD_DROP
    ]
    assert add_drops
    payload = add_drops[0].payload
    # p-fa-rb (14.0) beats p-rb2 (4.0), the weakest rostered RB.
    assert payload.add_player_id == "p-fa-rb"
    assert payload.drop_player_id == "p-rb2"
    # FCFS league: no bid amount is invented.
    assert payload.bid_amount is None


def test_walking_the_whole_pipeline_submits_nothing_without_approval(context) -> None:
    """The invariant, end to end.

    Ingest everything, run every analysis engine, list what is pending -- and
    confirm not one POST reached MFL.
    """
    ingest_everything(context)
    context.run_lineup_analysis(week=WEEK)
    context.run_waiver_analysis(week=WEEK)
    context.run_trade_analysis(week=WEEK)

    assert context.store.pending(), "the run should have produced recommendations"

    methods = {r.get("url") for r in context.client._client.requests}
    assert methods, "requests were made"
    # FakeMFLTransport.post raises; reaching here proves none was attempted.
    assert context.repos.audit_entries() == [], "no write was attempted"
    assert all(
        r.status == RecommendationStatus.PROPOSED for r in context.store.all()
    ), "nothing may advance past 'proposed' on its own"


def test_approval_mints_a_token_bound_to_the_shown_payload(context) -> None:
    ingest_everything(context)
    context.run_lineup_analysis(week=WEEK)
    recommendation = context.store.pending()[0]

    decision = context.channel.approve(recommendation.id, actor="integration-test")

    assert decision.authorises_execution
    assert decision.token.payload_hash == recommendation.payload_hash
    context.tokens.verify(decision.token, recommendation.payload_hash)


def test_execution_is_blocked_because_the_endpoint_is_unverified(context) -> None:
    """The last line of defence: even an approved action will not fire against
    an endpoint that has not been reconciled with MFL's documentation."""
    from mflbot.errors import EndpointNotVerifiedError
    from mflbot.mfl.write_client import MFLWriteClient

    ingest_everything(context)
    context.run_lineup_analysis(week=WEEK)
    recommendation = context.store.pending()[0]
    decision = context.channel.approve(recommendation.id)

    write_client = MFLWriteClient(
        context.config.league,
        context.client.auth,
        context.tokens,
        registry=context.registry,
        transport=context.client._client,  # would raise if a POST were attempted
    )
    with pytest.raises(EndpointNotVerifiedError, match="verify-endpoints"):
        write_client.submit(recommendation.payload, decision.token)


def test_caching_keeps_repeat_analysis_off_the_network(context) -> None:
    ingest_everything(context)
    before = len(context.client._client.requests)

    context.run_lineup_analysis(week=WEEK)
    context.run_lineup_analysis(week=WEEK)

    after = len(context.client._client.requests)
    assert after - before <= 3, (
        f"repeat analysis generated {after - before} requests; TTLs should have "
        f"served these from cache"
    )
