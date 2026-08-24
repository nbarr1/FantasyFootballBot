"""Starting-lineup optimiser.

Solves for the highest-projected legal lineup given the slot structure read from
the league export. Two solvers:

* **ILP** (via ``pulp``, an optional dependency) -- exact for any slot
  structure, including overlapping flex definitions.
* **Greedy** fallback -- fills the most restrictive slots first. That is
  provably optimal only when slot eligibility sets form a *laminar family*
  (every pair is either disjoint or nested), which is the shape almost every
  fantasy league uses: strict positional slots, then FLEX ⊇ {RB,WR,TE}, then
  SUPERFLEX ⊇ FLEX ∪ {QB}. When the structure is not laminar, the greedy
  solver **refuses to answer** rather than returning a lineup that may be
  beatable. Install the ``solver`` extra to handle those leagues.

Every starter carries risk flags, and the result is diffed against what is
currently submitted so the user is shown changes, not a wall of confirmations.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from ..domain.models import LineupSlot, Player
from ..errors import Missing

log = logging.getLogger(__name__)

#: Injury designations that make a player a bad or illegal start.
OUT_STATUSES = frozenset({"OUT", "IR", "INJURED_RESERVE", "SUSPENDED", "NOT_ACTIVE"})
DOUBTFUL_STATUSES = frozenset({"DOUBTFUL"})
QUESTIONABLE_STATUSES = frozenset({"QUESTIONABLE", "PROBABLE"})


@dataclass(frozen=True, slots=True)
class Candidate:
    """A rostered player who could be started, with their projection."""

    player: Player
    projection: float
    #: Kickoff time, when known. Drives the "starts after the lineup locks"
    #: warning; None means unknown, and the flag is simply not raised.
    kickoff: datetime | None = None
    injury_status: str | None = None
    on_bye: bool = False

    @property
    def player_id(self) -> str:
        return self.player.player_id

    @property
    def position(self) -> str | None:
        return self.player.position


@dataclass(frozen=True, slots=True)
class SlotAssignment:
    slot: LineupSlot
    candidate: Candidate
    risks: tuple[str, ...] = ()


@dataclass(slots=True)
class LineupSolution:
    assignments: list[SlotAssignment] = field(default_factory=list)
    total_projection: float = 0.0
    solver: str = "greedy"
    #: Roster players with no projection, which is why they were not considered.
    unprojected: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)

    @property
    def starter_ids(self) -> set[str]:
        return {a.candidate.player_id for a in self.assignments}

    def urgent_risks(self) -> list[str]:
        return [r for a in self.assignments for r in a.risks if r.startswith("OUT")]


@dataclass(frozen=True, slots=True)
class LineupChange:
    slot: LineupSlot
    player_in: Candidate
    player_out: Player | None
    projection_delta: float | None
    reason: str


def _is_laminar(slots: Sequence[LineupSlot]) -> bool:
    """True when every pair of eligibility sets is disjoint or nested."""
    sets = [frozenset(s.eligible_positions) for s in slots]
    for i, a in enumerate(sets):
        for b in sets[i + 1 :]:
            if a & b and not (a <= b or b <= a):
                return False
    return True


def _expand_slots(slots: Sequence[LineupSlot]) -> list[LineupSlot]:
    """Expand ``2 RB`` into two single-seat slots, so each seat is assigned once.

    Range slots (``1-2 FLEX``) are expanded to their minimum, because filling
    only the required seats is the legal-lineup question. Optional extra seats
    are surfaced as a caveat by the caller if they exist.
    """
    expanded: list[LineupSlot] = []
    for slot in slots:
        for seat in range(max(slot.min_starters, 0)):
            expanded.append(
                LineupSlot(
                    index=len(expanded),
                    name=slot.name if slot.min_starters == 1 else f"{slot.name}#{seat + 1}",
                    eligible_positions=slot.eligible_positions,
                    min_starters=1,
                    max_starters=1,
                )
            )
    return expanded


def solve_greedy(
    slots: Sequence[LineupSlot], candidates: Sequence[Candidate]
) -> list[tuple[LineupSlot, Candidate]] | Missing:
    """Fill the most restrictive slots first. Optimal for laminar structures."""
    if not _is_laminar(slots):
        return Missing(
            "this league's flex structure has overlapping, non-nested slot "
            "eligibility, which the greedy solver cannot solve optimally",
            {
                "remedy": "pip install 'mflbot[solver]' to enable the exact ILP solver",
                "slots": [f"{s.name}:{'/'.join(s.eligible_positions)}" for s in slots],
            },
        )

    remaining = sorted(candidates, key=lambda c: -c.projection)
    used: set[str] = set()
    # Most restrictive first: a slot that accepts fewer of the available players
    # must be filled before a slot that could take anyone.
    def restrictiveness(slot: LineupSlot) -> int:
        return sum(1 for c in candidates if slot.accepts(c.position))

    result: list[tuple[LineupSlot, Candidate]] = []
    for slot in sorted(slots, key=restrictiveness):
        pick = next(
            (c for c in remaining if c.player_id not in used and slot.accepts(c.position)),
            None,
        )
        if pick is None:
            continue
        used.add(pick.player_id)
        result.append((slot, pick))
    return result


def solve_ilp(
    slots: Sequence[LineupSlot], candidates: Sequence[Candidate]
) -> list[tuple[LineupSlot, Candidate]] | Missing:
    """Exact assignment via integer programming. Requires ``pulp``."""
    try:
        import pulp
    except ImportError:
        return Missing(
            "the ILP solver is not installed",
            {"remedy": "pip install 'mflbot[solver]'"},
        )

    problem = pulp.LpProblem("lineup", pulp.LpMaximize)
    variables: dict[tuple[int, str], object] = {}
    for slot in slots:
        for candidate in candidates:
            if slot.accepts(candidate.position):
                variables[(slot.index, candidate.player_id)] = pulp.LpVariable(
                    f"x_{slot.index}_{candidate.player_id}", cat="Binary"
                )

    if not variables:
        return []

    problem += pulp.lpSum(
        variables[(slot.index, c.player_id)] * c.projection
        for slot in slots
        for c in candidates
        if (slot.index, c.player_id) in variables
    )
    # Each seat holds at most one player.
    for slot in slots:
        seat_vars = [
            variables[(slot.index, c.player_id)]
            for c in candidates
            if (slot.index, c.player_id) in variables
        ]
        if seat_vars:
            problem += pulp.lpSum(seat_vars) <= 1
    # Each player occupies at most one seat.
    for candidate in candidates:
        player_vars = [
            variables[(slot.index, candidate.player_id)]
            for slot in slots
            if (slot.index, candidate.player_id) in variables
        ]
        if player_vars:
            problem += pulp.lpSum(player_vars) <= 1

    status = problem.solve(pulp.PULP_CBC_CMD(msg=False))
    if pulp.LpStatus[status] != "Optimal":
        return Missing(
            "the lineup solver did not reach an optimal solution",
            {"status": pulp.LpStatus[status]},
        )

    by_id = {c.player_id: c for c in candidates}
    by_index = {s.index: s for s in slots}
    out: list[tuple[LineupSlot, Candidate]] = []
    for (slot_index, player_id), var in variables.items():
        if var.value() and round(var.value()) == 1:
            out.append((by_index[slot_index], by_id[player_id]))
    return out


def assess_risks(
    candidate: Candidate, lock_time: datetime | None, escalate_within_hours: float
) -> tuple[str, ...]:
    """Per-starter risk flags. Each is a fact, not a prediction."""
    risks: list[str] = []
    status = (candidate.injury_status or "").upper()
    if status in OUT_STATUSES:
        risks.append(f"OUT: designated {candidate.injury_status} and should not start")
    elif status in DOUBTFUL_STATUSES:
        risks.append(f"doubtful: designated {candidate.injury_status}")
    elif status in QUESTIONABLE_STATUSES:
        risks.append(f"questionable: designated {candidate.injury_status}")
    if candidate.on_bye:
        risks.append("OUT: on bye this week and will score nothing")
    if lock_time is not None and candidate.kickoff is not None:
        if candidate.kickoff > lock_time:
            risks.append(
                f"kicks off at {candidate.kickoff:%a %H:%M UTC}, after the lineup "
                f"locks at {lock_time:%a %H:%M UTC} -- no substitution possible later"
            )
    return tuple(risks)


def optimise_lineup(
    slots: Sequence[LineupSlot],
    candidates: Sequence[Candidate],
    *,
    lock_time: datetime | None = None,
    escalate_within_hours: float = 3.0,
    prefer_ilp: bool = True,
) -> LineupSolution | Missing:
    """Compute the highest-projected legal lineup."""
    if not slots:
        return Missing(
            "the league's starting lineup structure is unknown",
            {"remedy": "run `bot sync-config` once credentials are configured"},
        )

    startable = [c for c in candidates if not c.on_bye]
    if not startable:
        return Missing(
            "no rostered player has a projection for this week",
            {"roster_size": len(candidates)},
        )

    expanded = _expand_slots(slots)
    solution_pairs: list[tuple[LineupSlot, Candidate]] | Missing
    solver_used = "ilp"
    if prefer_ilp:
        solution_pairs = solve_ilp(expanded, startable)
        if isinstance(solution_pairs, Missing):
            log.info("ILP unavailable (%s); falling back to greedy", solution_pairs.reason)
            solution_pairs = solve_greedy(expanded, startable)
            solver_used = "greedy"
    else:
        solution_pairs = solve_greedy(expanded, startable)
        solver_used = "greedy"

    if isinstance(solution_pairs, Missing):
        return solution_pairs

    assignments = [
        SlotAssignment(
            slot=slot,
            candidate=candidate,
            risks=assess_risks(candidate, lock_time, escalate_within_hours),
        )
        for slot, candidate in solution_pairs
    ]
    assignments.sort(key=lambda a: a.slot.index)

    solution = LineupSolution(
        assignments=assignments,
        total_projection=round(sum(a.candidate.projection for a in assignments), 3),
        solver=solver_used,
    )

    unfilled = len(expanded) - len(assignments)
    if unfilled > 0:
        solution.caveats.append(
            f"{unfilled} required lineup seat(s) could not be filled from the "
            f"projected roster -- check for an illegal or short roster."
        )
    return solution


def diff_lineup(
    solution: LineupSolution, submitted_ids: Iterable[str], player_lookup: dict[str, Player]
) -> list[LineupChange]:
    """Report only what would change, with a reason for each change."""
    submitted = set(submitted_ids)
    changes: list[LineupChange] = []
    displaced = [pid for pid in submitted if pid not in solution.starter_ids]
    for assignment in solution.assignments:
        if assignment.candidate.player_id in submitted:
            continue
        out_id = displaced.pop(0) if displaced else None
        reasons = list(assignment.risks)
        reason = (
            f"projected {assignment.candidate.projection:.1f} pts in the "
            f"{assignment.slot.name} slot"
        )
        if reasons:
            reason += "; " + "; ".join(reasons)
        changes.append(
            LineupChange(
                slot=assignment.slot,
                player_in=assignment.candidate,
                player_out=player_lookup.get(out_id) if out_id else None,
                projection_delta=None,
                reason=reason,
            )
        )
    return changes
