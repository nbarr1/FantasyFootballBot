"""Scoring rule parsing.

All inputs here are SYNTHETIC shapes exercising the parser's branches. They are
not any real league's scoring.
"""

from __future__ import annotations

from mflbot.analysis.rules_parser import FLAT, PER_UNIT, mfl_text, parse_rules


def _rules(*rule_nodes, positions="QB"):
    return {
        "rules": {
            "positionRules": [
                {"positions": {"$t": positions}, "rule": list(rule_nodes)}
            ]
        }
    }


def _rule(event, points, range_expr=None):
    node = {"event": {"$t": event}, "points": {"$t": points}}
    if range_expr is not None:
        node["range"] = {"$t": range_expr}
    return node


def test_mfl_text_unwraps_both_response_shapes() -> None:
    assert mfl_text({"$t": " PY "}) == "PY"
    assert mfl_text("PY") == "PY"
    assert mfl_text(42) == "42"
    assert mfl_text(None) is None


def test_per_unit_and_flat_rules_parse() -> None:
    parsed = parse_rules(_rules(_rule("XA", "*.04", "0-9999"), _rule("XB", "6", "0-99")))
    assert parsed.is_complete
    per_unit, flat = parsed.rules
    assert (per_unit.kind, per_unit.coefficient) == (PER_UNIT, 0.04)
    assert (flat.kind, flat.coefficient) == (FLAT, 6.0)


def test_negative_and_bare_decimal_points_parse() -> None:
    parsed = parse_rules(_rules(_rule("XA", "-2"), _rule("XB", "*-0.5"), _rule("XC", ".5")))
    assert parsed.is_complete
    assert [r.coefficient for r in parsed.rules] == [-2.0, -0.5, 0.5]


def test_open_ended_range_parses() -> None:
    parsed = parse_rules(_rules(_rule("XA", "3", "100+")))
    assert parsed.is_complete
    rule = parsed.rules[0]
    assert rule.range_low == 100.0 and rule.range_high is None


def test_unparseable_points_expression_becomes_a_gap_not_a_guess() -> None:
    parsed = parse_rules(_rules(_rule("XA", "*.04"), _rule("XB", "if(x>3,6,0)")))
    assert not parsed.is_complete
    assert len(parsed.rules) == 1
    assert len(parsed.gaps) == 1
    assert "if(x>3,6,0)" in parsed.gaps[0].describe()


def test_unrecognised_range_becomes_a_gap() -> None:
    parsed = parse_rules(_rules(_rule("XA", "*.04", "0..9999")))
    assert not parsed.is_complete
    assert "not recognised" in parsed.gaps[0].reason


def test_missing_event_code_becomes_a_gap() -> None:
    parsed = parse_rules(_rules({"points": {"$t": "6"}}))
    assert not parsed.is_complete
    assert "no event code" in parsed.gaps[0].reason


def test_empty_rules_export_is_a_gap_not_an_empty_success() -> None:
    """Zero rules must not read as 'this league scores nothing'."""
    parsed = parse_rules({"rules": {}})
    assert not parsed.is_complete
    assert parsed.gaps


def test_single_rule_returned_as_object_not_list() -> None:
    """MFL collapses one-element lists; the parser must cope."""
    payload = {
        "rules": {
            "positionRules": {
                "positions": {"$t": "QB"},
                "rule": {"event": {"$t": "XA"}, "points": {"$t": "*.04"}},
            }
        }
    }
    parsed = parse_rules(payload)
    assert parsed.is_complete and len(parsed.rules) == 1
