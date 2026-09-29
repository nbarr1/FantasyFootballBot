"""Lineup analysis helpers on the context: the current week, the submitted
lineup, and escalation.

All ids and payloads here are SYNTHETIC.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from mflbot.context import BotContext, submitted_starters
from mflbot.domain.models import Player


class ScheduleClient:
    """Answers nflSchedule with whatever week it is told, and counts asks."""

    def __init__(self, week: str) -> None:
        self.week = week
        self.calls: list[dict] = []

    def export(self, type_name, **kwargs):
        self.calls.append({"type": type_name, **kwargs})
        return SimpleNamespace(payload={"nflSchedule": {"week": self.week}})


def test_the_current_week_is_asked_for_again_once_it_is_stale(repos) -> None:
    client = ScheduleClient("3")
    context = SimpleNamespace(repos=repos, client=client)
    assert BotContext.current_week(context) == 3
    assert BotContext.current_week(context) == 3
    assert len(client.calls) == 1, "a fresh value should be reused"

    client.week = "4"
    repos.db.execute(
        "UPDATE ingest_state SET updated_at=? WHERE key='current_week'",
        ((datetime.now(UTC) - timedelta(hours=7)).isoformat(),),
    )
    assert BotContext.current_week(context) == 4
    assert client.calls[-1].get("force_refresh") is True, (
        "the response cache must not serve last week's schedule"
    )


def test_submitted_starters_are_this_franchises_only() -> None:
    payload = {
        "weeklyResults": {
            "matchup": [
                {
                    "franchise": [
                        {"id": "0001", "player": [
                            {"id": "p-ours", "status": "starter"},
                            {"id": "p-bench", "status": "nonstarter"},
                        ]},
                        {"id": "0002", "player": [
                            {"id": "p-theirs", "status": "starter"},
                        ]},
                    ]
                }
            ]
        }
    }
    assert submitted_starters(payload, "0001") == ["p-ours"]
    assert submitted_starters(payload, "0009") == []


def _escalations(deadline, submitted, injuries, hours=3.0):
    context = SimpleNamespace(
        config=SimpleNamespace(lineup=SimpleNamespace(escalate_within_hours=hours))
    )
    lookup = {"p-rb1": Player("p-rb1", "Synthetic RB One", "RB", "AAA")}
    settings = SimpleNamespace(lineup_deadline=deadline)
    return BotContext._escalations(context, submitted, lookup, injuries, {"AAA"}, settings)


def test_an_out_player_in_the_submitted_lineup_escalates_near_lock() -> None:
    soon = datetime.now(UTC) + timedelta(hours=1)
    lines = _escalations(soon, ["p-rb1"], {"p-rb1": "Out"})
    assert lines and "Synthetic RB One" in lines[0]


def test_escalation_waits_until_lock_is_within_the_configured_window() -> None:
    later = datetime.now(UTC) + timedelta(hours=30)
    assert _escalations(later, ["p-rb1"], {"p-rb1": "Out"}) == []
