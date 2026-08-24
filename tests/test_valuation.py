"""The valuation core.

Inputs are SYNTHETIC event codes (XA, XB...) and SYNTHETIC coefficients. Using
invented event codes rather than real MFL abbreviations is deliberate: it proves
the code is driven by the parsed rules rather than by any built-in knowledge of
what "PY" means.
"""

from __future__ import annotations

import pytest

from mflbot.analysis.rules_parser import parse_rules
from mflbot.analysis.valuation import build_scoring_model
from mflbot.domain.models import StatLine
from mflbot.errors import Missing


def model_from(*rule_nodes, positions="QB"):
    payload = {
        "rules": {
            "positionRules": [
                {"positions": {"$t": positions}, "rule": list(rule_nodes)}
            ]
        }
    }
    return build_scoring_model(parse_rules(payload))


def rule(event, points, range_expr=None):
    node = {"event": {"$t": event}, "points": {"$t": points}}
    if range_expr is not None:
        node["range"] = {"$t": range_expr}
    return node


def test_per_unit_scoring_multiplies() -> None:
    model = model_from(rule("XA", "*0.1", "0-9999"))
    result = model.score(StatLine("p1", 1, {"XA": 250.0}), "QB")
    assert result.total == pytest.approx(25.0)


def test_flat_rule_awards_once_regardless_of_count() -> None:
    model = model_from(rule("XB", "6", "0-99"))
    result = model.score(StatLine("p1", 1, {"XB": 3.0}), "QB")
    assert result.total == pytest.approx(6.0)


def test_rules_combine_and_the_breakdown_explains_the_total() -> None:
    model = model_from(rule("XA", "*0.1", "0-9999"), rule("XB", "*4"))
    result = model.score(StatLine("p1", 1, {"XA": 300.0, "XB": 2.0}), "QB")
    assert result.total == pytest.approx(38.0)
    assert sum(c.points for c in result.components) == pytest.approx(result.total)
    assert {c.event_code for c in result.components} == {"XA", "XB"}


def test_negative_coefficients_subtract() -> None:
    model = model_from(rule("XA", "*0.1"), rule("XC", "*-2"))
    result = model.score(StatLine("p1", 1, {"XA": 100.0, "XC": 3.0}), "QB")
    assert result.total == pytest.approx(4.0)


def test_zero_valued_events_contribute_nothing() -> None:
    model = model_from(rule("XA", "*0.1"))
    result = model.score(StatLine("p1", 1, {"XA": 0.0}), "QB")
    assert result.total == 0.0 and result.components == ()


def test_flat_bonus_only_applies_inside_its_range() -> None:
    model = model_from(rule("XA", "*0.1"), rule("XBONUS", "3", "100+"))
    below = model.score(StatLine("p1", 1, {"XA": 90.0, "XBONUS": 90.0}), "QB")
    above = model.score(StatLine("p1", 1, {"XA": 120.0, "XBONUS": 120.0}), "QB")
    assert below.total == pytest.approx(9.0)
    assert above.total == pytest.approx(15.0)


def test_value_inside_the_band_is_not_flagged_ambiguous() -> None:
    """The common case -- a full-range rule -- must produce no noise."""
    model = model_from(rule("XA", "*0.1", "0-9999"))
    result = model.score(StatLine("p1", 1, {"XA": 300.0}), "QB")
    assert result.ambiguities == ()


def test_value_outside_a_bounded_band_is_flagged_not_silently_clipped() -> None:
    model = model_from(rule("XA", "*0.1", "0-100"))
    result = model.score(StatLine("p1", 1, {"XA": 250.0}), "QB")
    assert result.total == pytest.approx(10.0)
    assert result.ambiguities, "clipping must be disclosed, not hidden"


def test_unparsed_rule_blocks_scoring_for_that_position() -> None:
    model = model_from(rule("XA", "*0.1"), rule("XB", "if(x)"))
    result = model.score(StatLine("p1", 1, {"XA": 300.0}), "QB")
    assert isinstance(result, Missing)
    assert "could not be fully parsed" in result.reason


def test_a_gap_in_one_position_does_not_block_an_unrelated_position() -> None:
    payload = {
        "rules": {
            "positionRules": [
                {"positions": {"$t": "QB"}, "rule": [rule("XA", "broken")]},
                {"positions": {"$t": "RB"}, "rule": [rule("XA", "*0.1")]},
            ]
        }
    }
    model = build_scoring_model(parse_rules(payload))
    assert isinstance(model.score(StatLine("p1", 1, {"XA": 100.0}), "QB"), Missing)
    assert model.score(StatLine("p2", 1, {"XA": 100.0}), "RB").total == pytest.approx(10.0)


def test_position_with_no_rules_returns_missing_not_zero() -> None:
    """Zero points and 'we have no rules for kickers' are different claims."""
    model = model_from(rule("XA", "*0.1"), positions="QB")
    result = model.score(StatLine("p1", 1, {"XA": 100.0}), "PK")
    assert isinstance(result, Missing)
    assert "no scoring rules apply" in result.reason


def test_rule_without_position_restriction_applies_to_everyone() -> None:
    model = model_from(rule("XA", "*0.1"), positions="")
    assert model.score(StatLine("p1", 1, {"XA": 100.0}), "TE").total == pytest.approx(10.0)
