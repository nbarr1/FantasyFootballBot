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

A slot may allow a range of starters (``RB 2-4``). Its minimum is always
filled; the seats above the minimum are *optional*, and the league's total
starter count decides how many of them are filled in all. When that total is
unknown, only the minimums are filled and the solution says so -- it does not
guess at a lineup size.

A player designated OUT (or IR, suspended, inactive) is never started: they
score nothing, so the seat goes to the best healthy alternative, and the
solution lists who was benched and why.

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
    #: Projected players deliberately left out, each with the reason.
    benched: list[str] = field(default_factory=list)
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


def _expand_slots(
    slots: Sequence[LineupSlot],
) -> tuple[list[LineupSlot], list[LineupSlot]]:
    """Expand each slot into single seats: ``(required, optional)``.

    ``2 RB`` becomes two required RB seats, so each seat is assigned once.
    ``RB 2-4`` becomes two required seats and two optional ones; how many
    optional seats are filled in total is capped by the league's starter count
    (see :func:`optimise_lineup`).
    """
    required: list[LineupSlot] = []
    optional: list[LineupSlot] = []
    for slot in slots:
        low = max(slot.min_starters, 0)
        high = max(slot.max_starters, low)
        for seat in range(high):
            name = slot.name if high == 1 else f"{slot.name}#{seat + 1}"
            bucket = required if seat < low else optional
            bucket.append(
                LineupSlot(
                    index=0,  # renumbered below, once both lists are known
                    name=name,
                    eligible_positions=slot.eligible_positions,
                    min_starters=1,
                    max_starters=1,
                )
            )
    seats = [
        LineupSlot(i, s.name, s.eligible_positions, 1, 1)
        for i, s in enumerate(required + optional)
    ]
    return seats[: len(required)], seats[len(required):]


def _single_position_seats(seats: Sequence[LineupSlot]) -> bool:
    return all(len(set(s.eligible_positions)) == 1 for s in seats)


def solve_greedy(
    slots: Sequence[LineupSlot],
    candidates: Sequence[Candidate],
    *,
    optional: Sequence[LineupSlot] = (),
    extra: int = 0,
) -> list[tuple[LineupSlot, Candidate]] | Missing:
    """Fill the most restrictive slots first. Optimal for laminar structures.

    Optional seats (``extra`` of them, chosen from ``optional``) are then filled
    with the best remaining players. That second pass is provably optimal only
    when every seat takes a single position -- MFL's positional ranges -- so any
    other structure with optional seats is refused rather than approximated.
    """
    if not _is_laminar(slots):
        return Missing(
            "this league's flex structure has overlapping, non-nested slot "
            "eligibility, which the greedy solver cannot solve optimally",
            {
                "remedy": "pip install 'mflbot[solver]' to enable the exact ILP solver",
                "slots": [f"{s.name}:{'/'.join(s.eligible_positions)}" for s in slots],
            },
        )
    if extra > 0 and optional and not _single_position_seats([*slots, *optional]):
        return Missing(
            "this league combines flex slots with ranged slots, which the greedy "
            "solver cannot solve optimally",
            {"remedy": "pip install 'mflbot[solver]' to enable the exact ILP solver"},
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

    open_seats = list(optional)
    filled = 0
    for candidate in remaining:
        if filled >= extra:
            break
        if candidate.player_id in used:
            continue
        seat = next((s for s in open_seats if s.accepts(candidate.position)), None)
        if seat is None:
            continue
        open_seats.remove(seat)
        used.add(candidate.player_id)
        result.append((seat, candidate))
        filled += 1
    return result


def solve_ilp(
    slots: Sequence[LineupSlot],
    candidates: Sequence[Candidate],
    *,
    optional: Sequence[LineupSlot] = (),
    extra: int = 0,
) -> list[tuple[LineupSlot, Candidate]] | Missing:
    """Exact assignment via integer programming. Requires ``pulp``."""
    try:
        import pulp
    except ImportError:
        return Missing(
            "the ILP solver is not installed",
            {"remedy": "pip install 'mflbot[solver]'"},
        )
    try:
        return _solve_ilp(pulp, slots, candidates, optional, extra)
    except Exception as exc:  # noqa: BLE001 - a solver fault must fall back, not crash
        # An incompatible pulp release or a missing CBC binary lands here. The
        # caller falls back to the greedy solver, which refuses structures it
        # cannot solve exactly, so this never degrades into a worse lineup.
        return Missing("the ILP solver failed", {"error": f"{type(exc).__name__}: {exc}"})


def _solve_ilp(pulp, slots, candidates, optional, extra):
    seats = [*slots, *optional]
    optional_indexes = {s.index for s in optional}
    problem = pulp.LpProblem("lineup", pulp.LpMaximize)
    variables: dict[tuple[int, str], object] = {}
    for slot in seats:
        for candidate in candidates:
            if slot.accepts(candidate.position):
                variables[(slot.index, candidate.player_id)] = pulp.LpVariable(
                    f"x_{slot.index}_{candidate.player_id}", cat="Binary"
                )

    if not variables:
        return []

    problem += pulp.lpSum(
        variables[(slot.index, c.player_id)] * c.projection
        for slot in seats
        for c in candidates
        if (slot.index, c.player_id) in variables
    )
    # Each seat holds at most one player.
    for slot in seats:
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
            for slot in seats
            if (slot.index, candidate.player_id) in variables
        ]
        if player_vars:
            problem += pulp.lpSum(player_vars) <= 1
    # No more optional seats than the league's starter count leaves room for.
    optional_vars = [v for (index, _), v in variables.items() if index in optional_indexes]
    if optional_vars:
        problem += pulp.lpSum(optional_vars) <= extra

    status = problem.solve(pulp.PULP_CBC_CMD(msg=False))
    if pulp.LpStatus[status] != "Optimal":
        return Missing(
            "the lineup solver did not reach an optimal solution",
            {"status": pulp.LpStatus[status]},
        )

    by_id = {c.player_id: c for c in candidates}
    by_index = {s.index: s for s in seats}
    out: list[tuple[LineupSlot, Candidate]] = []
    for (slot_index, player_id), var in variables.items():
        if var.value() and round(var.value()) == 1:
            out.append((by_index[slot_index], by_id[player_id]))
    return out


def is_designated_out(injury_status: str | None) -> bool:
    return (injury_status or "").upper() in OUT_STATUSES


def assess_risks(candidate: Candidate, lock_time: datetime | None) -> tuple[str, ...]:
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
    starter_count: int | None = None,
    lock_time: datetime | None = None,
    prefer_ilp: bool = True,
) -> LineupSolution | Missing:
    """Compute the highest-projected legal lineup.

    ``starter_count`` is the league's total number of starters. It matters only
    when a slot allows a range: it decides how many seats above the slot
    minimums are filled.
    """
    if not slots:
        return Missing(
            "the league's starting lineup structure is unknown",
            {"remedy": "run `bot sync-config` once credentials are configured"},
        )

    benched = [
        f"{c.player.display} (designated {c.injury_status})"
        for c in candidates
        if not c.on_bye and is_designated_out(c.injury_status)
    ]
    startable = [
        c for c in candidates if not c.on_bye and not is_designated_out(c.injury_status)
    ]
    if not startable:
        return Missing(
            "no rostered player is available to start this week",
            {"roster_size": len(candidates), "benched": benched},
        )

    required, optional = _expand_slots(slots)
    caveats: list[str] = []
    extra = 0
    if starter_count is None:
        if optional:
            caveats.append(
                f"{len(optional)} lineup seat(s) are optional (a slot allows more "
                f"starters than its minimum), and the league's total starter count "
                f"is unknown, so only the minimum {len(required)} were filled. Check "
                f"`bot config-summary`; you may be able to start more."
            )
    elif starter_count < len(required):
        caveats.append(
            f"The league reports {starter_count} starters, fewer than the "
            f"{len(required)} its slot minimums require; only the minimums were "
            f"filled. Check `bot config-summary`."
        )
    else:
        extra = min(starter_count - len(required), len(optional))
        if starter_count > len(required) + len(optional):
            caveats.append(
                f"The league reports {starter_count} starters, but the parsed slots "
                f"allow at most {len(required) + len(optional)}. Check "
                f"`bot config-summary` -- a slot may not have been parsed."
            )

    solution_pairs: list[tuple[LineupSlot, Candidate]] | Missing
    solver_used = "ilp"
    if prefer_ilp:
        solution_pairs = solve_ilp(required, startable, optional=optional, extra=extra)
        if isinstance(solution_pairs, Missing):
            log.info("ILP unavailable (%s); falling back to greedy", solution_pairs)
            solution_pairs = solve_greedy(required, startable, optional=optional, extra=extra)
            solver_used = "greedy"
    else:
        solution_pairs = solve_greedy(required, startable, optional=optional, extra=extra)
        solver_used = "greedy"

    if isinstance(solution_pairs, Missing):
        return solution_pairs

    assignments = [
        SlotAssignment(
            slot=slot,
            candidate=candidate,
            risks=assess_risks(candidate, lock_time),
        )
        for slot, candidate in solution_pairs
    ]
    assignments.sort(key=lambda a: a.slot.index)

    solution = LineupSolution(
        assignments=assignments,
        total_projection=round(sum(a.candidate.projection for a in assignments), 3),
        solver=solver_used,
        benched=benched,
        caveats=caveats,
    )

    optional_indexes = {s.index for s in optional}
    required_filled = sum(1 for a in assignments if a.slot.index not in optional_indexes)
    optional_filled = len(assignments) - required_filled
    unfilled = (len(required) - required_filled) + (extra - optional_filled)
    if unfilled > 0:
        solution.caveats.append(
            f"{unfilled} lineup seat(s) could not be filled from the projected, "
            f"available roster -- check for an illegal or short roster."
        )
    if benched:
        solution.caveats.append(
            "Benched because they will not play: " + ", ".join(benched) + "."
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
