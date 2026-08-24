"""The valuation core: a stat line to fantasy points, under *this* league's rules.

Every downstream number -- lineup ranking, waiver value, trade fairness --
routes through :meth:`ScoringModel.score`. That is deliberate: when the
commissioner edits scoring mid-season, the daily ``rules`` refresh updates one
object and every recommendation moves with it, instead of a scoring assumption
being duplicated across three engines and drifting.

The model refuses to score a position whose rules it could not fully parse.
:class:`~mflbot.errors.Missing` comes back instead of a number, and the caller
surfaces the gap. There is no PPR fallback, no "standard scoring" default, and
no silently-skipped rule.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..domain.models import StatLine
from ..errors import Missing
from .rules_parser import FLAT, PER_UNIT, ParsedRules, ScoringRule


@dataclass(frozen=True, slots=True)
class ScoreComponent:
    """One rule's contribution, kept so a total can always be explained."""

    event_code: str
    event_value: float
    rule_index: int
    points: float
    note: str | None = None


@dataclass(frozen=True, slots=True)
class ScoreBreakdown:
    total: float
    components: tuple[ScoreComponent, ...] = ()
    #: Computations where MFL's own semantics are genuinely ambiguous (see
    #: :meth:`ScoringModel.score`). Non-empty means the number is usable but
    #: should be presented with a caveat, not as settled fact.
    ambiguities: tuple[str, ...] = ()

    def explain(self) -> str:
        lines = [f"{self.total:.2f} pts"]
        for c in sorted(self.components, key=lambda c: -abs(c.points)):
            suffix = f"  [{c.note}]" if c.note else ""
            lines.append(
                f"  {c.points:+7.2f}  {c.event_code} x{c.event_value:g}{suffix}"
            )
        lines.extend(f"  ! {a}" for a in self.ambiguities)
        return "\n".join(lines)


@dataclass(slots=True)
class ScoringModel:
    """A league's parsed scoring rules, ready to evaluate stat lines."""

    parsed: ParsedRules
    #: Event codes MFL's catalogue describes, used only to explain gaps well.
    known_events: frozenset[str] = field(default_factory=frozenset)

    @property
    def is_complete(self) -> bool:
        return self.parsed.is_complete

    def gaps_for_position(self, position: str | None) -> tuple[str, ...]:
        """Unparsed rules that would affect scoring for ``position``."""
        out = []
        for gap in self.parsed.gaps:
            if not gap.positions or (position is not None and position in gap.positions):
                out.append(gap.describe())
        return tuple(out)

    def rules_for_position(self, position: str | None) -> tuple[ScoringRule, ...]:
        return tuple(r for r in self.parsed.rules if r.applies_to_position(position))

    def score(self, stat_line: StatLine, position: str | None) -> ScoreBreakdown | Missing:
        """Convert a stat line into points, or explain why it cannot.

        Returns :class:`~mflbot.errors.Missing` when any rule affecting this
        position failed to parse. Partial scoring is not offered: a total that
        silently omits a rule is indistinguishable from a correct one at the
        point of use, which is exactly the failure mode this bot must not have.
        """
        gaps = self.gaps_for_position(position)
        if gaps:
            return Missing(
                "scoring rules for this position could not be fully parsed",
                {"position": position, "gaps": list(gaps)},
            )

        applicable = self.rules_for_position(position)
        if not applicable:
            return Missing(
                "no scoring rules apply to this position",
                {"position": position,
                 "hint": "either the league does not score this position, or the "
                         "rules export uses position codes this bot has not seen"},
            )

        components: list[ScoreComponent] = []
        ambiguities: list[str] = []
        total = 0.0

        for rule in applicable:
            value = stat_line.get(rule.event_code)
            if value == 0.0:
                continue

            if rule.kind == FLAT:
                if not rule.in_range(value):
                    continue
                points = rule.coefficient or 0.0
                components.append(
                    ScoreComponent(rule.event_code, value, rule.index, points)
                )
                total += points
                continue

            if rule.kind == PER_UNIT:
                coefficient = rule.coefficient or 0.0
                low = rule.range_low
                high = rule.range_high
                # Band semantics: only the portion of the value inside the rule's
                # range earns this coefficient. When the value sits entirely
                # inside the band -- the overwhelmingly common case -- this is
                # simply coefficient * value and there is nothing ambiguous.
                effective = value
                note = None
                clipped = False
                if low is not None and effective < low:
                    continue
                if high is not None and value > high:
                    effective = high
                    clipped = True
                if low is not None and low > 0:
                    effective = effective - low
                    clipped = True
                if clipped:
                    note = f"band {rule.range_expr}"
                    ambiguities.append(
                        f"{rule.event_code}: value {value:g} falls outside rule "
                        f"#{rule.index}'s range {rule.range_expr!r}; scored the "
                        f"in-band portion only. Confirm with `bot validate-scoring` "
                        f"once real weekly scores exist."
                    )
                points = coefficient * effective
                components.append(
                    ScoreComponent(rule.event_code, value, rule.index, points, note)
                )
                total += points

        return ScoreBreakdown(
            total=round(total, 4),
            components=tuple(components),
            ambiguities=tuple(ambiguities),
        )


def build_scoring_model(
    parsed: ParsedRules, known_events: set[str] | None = None
) -> ScoringModel:
    return ScoringModel(parsed=parsed, known_events=frozenset(known_events or ()))
