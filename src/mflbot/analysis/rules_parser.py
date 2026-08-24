"""Parser for MFL's ``export?TYPE=rules`` scoring definition.

MFL expresses a league's scoring as a list of position groups, each holding a
list of rules. A rule names a scoring *event* by abbreviation (the abbreviations
are catalogued by the separate ``allRules`` export, which is why no event code
is hardcoded in this repository), a *points* expression, and an optional
*range* restricting when the rule applies.

Two points expression forms are recognised:

``*0.04``
    Per-unit: award 0.04 points for each unit of the event.
``6`` / ``-2`` / ``0.5``
    Flat: award that many points once, when the event value falls in range.

Anything else is **not guessed at**. It is recorded as a
:class:`ScoringRuleGap`, and its presence makes the scoring model incomplete,
which blocks every scoring-dependent feature. This is the single most important
correctness property in the bot: a league whose scoring is misread produces
confident, wrong advice, which is worse than no advice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

PER_UNIT = "per_unit"
FLAT = "flat"
UNPARSED = "unparsed"

#: A points expression that multiplies: "*.04", "* 0.04", "*-1".
_PER_UNIT_RE = re.compile(r"^\*\s*(-?\d*\.?\d+)$")
#: A plain numeric award: "6", "-2", ".5".
_FLAT_RE = re.compile(r"^(-?\d*\.?\d+)$")
#: A range: "0-9999", "100-199", "100+", "-5--1".
_RANGE_RE = re.compile(r"^(-?\d*\.?\d+)\s*-\s*(-?\d*\.?\d+)$")
_OPEN_RANGE_RE = re.compile(r"^(-?\d*\.?\d+)\s*\+$")


def mfl_text(value: Any) -> str | None:
    """Unwrap MFL's XML-to-JSON text nodes.

    MFL renders element text as ``{"$t": "..."}`` in JSON mode, but returns
    bare strings in some places and for some endpoints. Both shapes appear in
    real responses, so every field read goes through here.
    """
    if value is None:
        return None
    if isinstance(value, dict):
        inner = value.get("$t")
        return None if inner is None else str(inner).strip()
    if isinstance(value, (str, int, float)):
        return str(value).strip()
    return None


@dataclass(frozen=True, slots=True)
class ScoringRule:
    index: int
    positions: tuple[str, ...]
    event_code: str
    points_expr: str
    range_expr: str | None
    kind: str
    coefficient: float | None
    range_low: float | None
    range_high: float | None

    def applies_to_position(self, position: str | None) -> bool:
        if not self.positions:
            # An empty position list means the rule was published without a
            # position restriction; MFL applies such rules to every scorer.
            return True
        return position is not None and position in self.positions

    def in_range(self, value: float) -> bool:
        if self.range_low is not None and value < self.range_low:
            return False
        if self.range_high is not None and value > self.range_high:
            return False
        return True

    @property
    def is_bounded_per_unit(self) -> bool:
        """A per-unit rule whose range does not cover everything.

        MFL's own semantics for these (does the coefficient apply to the whole
        value, or only the part inside the band?) are not stated in a way this
        parser can rely on. The valuation core uses band semantics and marks the
        result as needing validation -- see :meth:`ScoringModel.score`.
        """
        if self.kind != PER_UNIT:
            return False
        return (self.range_low not in (None, 0.0)) or (self.range_high is not None)


@dataclass(frozen=True, slots=True)
class ScoringRuleGap:
    """A rule MFL returned that this parser could not interpret."""

    index: int
    positions: tuple[str, ...]
    event_code: str | None
    points_expr: str | None
    range_expr: str | None
    reason: str

    def describe(self) -> str:
        return (
            f"rule #{self.index} [{','.join(self.positions) or 'all positions'}] "
            f"event={self.event_code!r} points={self.points_expr!r} "
            f"range={self.range_expr!r}: {self.reason}"
        )


@dataclass(frozen=True, slots=True)
class ParsedRules:
    rules: tuple[ScoringRule, ...] = ()
    gaps: tuple[ScoringRuleGap, ...] = ()

    @property
    def is_complete(self) -> bool:
        return not self.gaps and bool(self.rules)


def _parse_points(expr: str) -> tuple[str, float | None]:
    match = _PER_UNIT_RE.match(expr)
    if match:
        return PER_UNIT, float(match.group(1))
    match = _FLAT_RE.match(expr)
    if match:
        return FLAT, float(match.group(1))
    return UNPARSED, None


def _parse_range(expr: str | None) -> tuple[float | None, float | None, bool]:
    """Return ``(low, high, ok)``. ``ok`` is False for an unrecognised range."""
    if expr is None or expr == "":
        return None, None, True
    match = _RANGE_RE.match(expr)
    if match:
        return float(match.group(1)), float(match.group(2)), True
    match = _OPEN_RANGE_RE.match(expr)
    if match:
        return float(match.group(1)), None, True
    return None, None, False


def _iter_rule_nodes(payload: Any) -> Iterable[tuple[tuple[str, ...], Any]]:
    """Yield ``(positions, rule_node)`` pairs from a rules export payload."""
    root = payload.get("rules", payload) if isinstance(payload, dict) else payload
    if not isinstance(root, dict):
        return
    groups = root.get("positionRules", root.get("rule", []))
    if isinstance(groups, dict):
        groups = [groups]
    for group in groups or []:
        if not isinstance(group, dict):
            continue
        positions_text = mfl_text(group.get("positions")) or ""
        positions = tuple(p.strip() for p in positions_text.split(",") if p.strip())
        rule_nodes = group.get("rule", group)
        if isinstance(rule_nodes, dict):
            rule_nodes = [rule_nodes]
        for node in rule_nodes or []:
            if isinstance(node, dict):
                yield positions, node


def parse_rules(payload: Any) -> ParsedRules:
    """Parse a ``TYPE=rules`` payload into a scoring model.

    Never raises on a malformed rule: an unreadable rule becomes a gap, so the
    caller learns about *all* the problems at once instead of one per run.
    """
    rules: list[ScoringRule] = []
    gaps: list[ScoringRuleGap] = []

    for index, (positions, node) in enumerate(_iter_rule_nodes(payload)):
        event_code = mfl_text(node.get("event"))
        points_expr = mfl_text(node.get("points"))
        range_expr = mfl_text(node.get("range"))

        if not event_code:
            gaps.append(
                ScoringRuleGap(index, positions, event_code, points_expr, range_expr,
                               "rule has no event code")
            )
            continue
        if points_expr is None:
            gaps.append(
                ScoringRuleGap(index, positions, event_code, points_expr, range_expr,
                               "rule has no points expression")
            )
            continue

        kind, coefficient = _parse_points(points_expr)
        if kind == UNPARSED:
            gaps.append(
                ScoringRuleGap(
                    index, positions, event_code, points_expr, range_expr,
                    f"points expression {points_expr!r} is neither a per-unit "
                    f"multiplier (*N) nor a flat award (N)",
                )
            )
            continue

        low, high, ok = _parse_range(range_expr)
        if not ok:
            gaps.append(
                ScoringRuleGap(index, positions, event_code, points_expr, range_expr,
                               f"range expression {range_expr!r} is not recognised")
            )
            continue

        rules.append(
            ScoringRule(
                index=index,
                positions=positions,
                event_code=event_code,
                points_expr=points_expr,
                range_expr=range_expr,
                kind=kind,
                coefficient=coefficient,
                range_low=low,
                range_high=high,
            )
        )

    if not rules and not gaps:
        gaps.append(
            ScoringRuleGap(
                0, (), None, None, None,
                "the rules export contained no rules at all -- either the league "
                "has not published scoring yet, or the response shape differs "
                "from what this parser expects",
            )
        )
    return ParsedRules(tuple(rules), tuple(gaps))


def parse_rule_definitions(payload: Any) -> list[dict[str, Any]]:
    """Parse the ``allRules`` catalogue of event abbreviations.

    This is what lets the bot talk about "PY" or "TD" without a hardcoded table
    of event codes invented at development time.
    """
    root = payload.get("allRules", payload) if isinstance(payload, dict) else payload
    if not isinstance(root, dict):
        return []
    nodes = root.get("rule", [])
    if isinstance(nodes, dict):
        nodes = [nodes]
    out: list[dict[str, Any]] = []
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        code = mfl_text(node.get("abbreviation")) or mfl_text(node.get("event"))
        if not code:
            continue
        out.append(
            {
                "event_code": code,
                "short_name": mfl_text(node.get("shortDescription")),
                "description": mfl_text(node.get("detailedDescription"))
                or mfl_text(node.get("description")),
                "is_player": _flag(node.get("isPlayerRule")),
                "is_team": _flag(node.get("isTeamRule")),
                "is_coach": _flag(node.get("isCoachRule")),
            }
        )
    return out


def _flag(value: Any) -> int | None:
    text = mfl_text(value)
    if text is None:
        return None
    return 1 if text.strip().lower() in {"1", "true", "yes"} else 0
