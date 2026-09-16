"""Tests for differences/decisions.py (plan 3 task 1)."""

import pytest

from easymigrate.differences.decisions import (
    fingerprint,
    from_yaml,
    merge_decisions,
    readiness_problems,
    to_yaml,
)
from easymigrate.differences.model import (
    Decision,
    DecisionSet,
    Difference,
    DifferenceRule,
    Strategy,
)

_KEEP = Strategy(id="keep", title="Keep", effect="no change", risk="none")
_MANUAL = Strategy(id="manual", title="Manual", effect="omit", risk="low", manual=True)


def _rule(rule_id="TEST-RULE", strategies=(_KEEP, _MANUAL), default="keep"):
    attrs = {
        "id": rule_id,
        "family": "test",
        "title": rule_id,
        "impact": "impact",
        "strategies": strategies,
        "default": default,
        "detect": lambda self, ir: [],
        "apply": lambda self, ir, diff, strategy_id: ir,
    }
    return type(f"_Rule_{rule_id.replace('-', '_')}", (DifferenceRule,), attrs)()


def _diff(subject="network:a", rule_id="TEST-RULE"):
    return Difference(key=f"{rule_id}@{subject}", rule_id=rule_id, subjects=[subject], context={})


_PARAM_STRATEGY = Strategy(id="ranged", title="Ranged", effect="picks a range", risk="medium")


def _param_rule(rule_id="PARAM-RULE"):
    def default_params(self, diff, strategy_id):
        if strategy_id == "ranged":
            return {"range": "10.0.0.0", "prefix_length": 24}
        return {}

    def validate_params(self, diff, strategy_id, params):
        if strategy_id == "ranged" and params.get("prefix_length") not in range(16, 25):
            raise ValueError("prefix_length must be between 16 and 24")

    attrs = {
        "id": rule_id,
        "family": "test",
        "title": rule_id,
        "impact": "impact",
        "strategies": (_PARAM_STRATEGY, _MANUAL),
        "default": "ranged",
        "detect": lambda self, ir: [],
        "apply": lambda self, ir, diff, strategy_id, params=None: ir,
        "default_params": default_params,
        "validate_params": validate_params,
    }
    return type(f"_Rule_{rule_id.replace('-', '_')}", (DifferenceRule,), attrs)()


def test_fingerprint_depends_only_on_sorted_keys():
    d1 = _diff("network:a")
    d2 = _diff("network:b")
    assert fingerprint([d1, d2]) == fingerprint([d2, d1])
    assert fingerprint([d1]) != fingerprint([d1, d2])


def test_merge_decisions_fills_in_defaults():
    rule = _rule()
    diff = _diff()
    merged, dropped = merge_decisions(None, [diff], (rule,))
    assert dropped == []
    assert len(merged.decisions) == 1
    assert merged.decisions[0].strategy == "keep"
    assert merged.decisions[0].decided_by == "default"
    assert merged.differences_fingerprint == fingerprint([diff])


def test_merge_decisions_keeps_user_decisions():
    rule = _rule()
    diff = _diff()
    existing = DecisionSet(
        differences_fingerprint="stale",
        decisions=[Decision(key=diff.key, strategy="manual", decided_by="user", acknowledged=True)],
    )
    merged, dropped = merge_decisions(existing, [diff], (rule,))
    assert dropped == []
    assert merged.decisions[0].decided_by == "user"
    assert merged.decisions[0].strategy == "manual"
    assert merged.decisions[0].acknowledged is True


def test_merge_decisions_drops_orphaned_and_invalid_strategy_keys():
    rule = _rule()
    diff = _diff()
    orphan = Decision(key="TEST-RULE@network:gone", strategy="keep", decided_by="user")
    invalid_strategy = Decision(key=diff.key, strategy="no-such-strategy", decided_by="user")
    existing = DecisionSet(differences_fingerprint="stale", decisions=[orphan, invalid_strategy])
    merged, dropped = merge_decisions(existing, [diff], (rule,))
    assert dropped == sorted([orphan.key, diff.key])
    # the invalid-strategy decision was replaced with a fresh default
    assert merged.decisions[0].strategy == "keep"
    assert merged.decisions[0].decided_by == "default"


def test_readiness_problems_empty_when_ready():
    rule = _rule()
    diff = _diff()
    merged, _ = merge_decisions(None, [diff], (rule,))
    assert readiness_problems(merged, [diff], (rule,)) == []


def test_readiness_problems_missing_or_stale():
    rule = _rule()
    diff = _diff()
    assert readiness_problems(None, [diff], (rule,)) == [
        "differences changed since resolve; run `easymigrate resolve`"
    ]
    stale = DecisionSet(differences_fingerprint="wrong", decisions=[])
    assert readiness_problems(stale, [diff], (rule,)) == [
        "differences changed since resolve; run `easymigrate resolve`"
    ]


def test_readiness_problems_unknown_strategy():
    rule = _rule()
    diff = _diff()
    decisions = DecisionSet(
        differences_fingerprint=fingerprint([diff]),
        decisions=[Decision(key=diff.key, strategy="bogus", decided_by="user")],
    )
    problems = readiness_problems(decisions, [diff], (rule,))
    assert problems == [f"{diff.key} uses an unknown strategy 'bogus'"]


def test_readiness_problems_unacknowledged_manual():
    rule = _rule()
    diff = _diff()
    decisions = DecisionSet(
        differences_fingerprint=fingerprint([diff]),
        decisions=[
            Decision(key=diff.key, strategy="manual", decided_by="user", acknowledged=False)
        ],
    )
    problems = readiness_problems(decisions, [diff], (rule,))
    assert problems == [f"{diff.key} uses a manual strategy and is not acknowledged"]


def test_yaml_round_trip():
    diff = _diff()
    decisions = DecisionSet(
        differences_fingerprint=fingerprint([diff]),
        decisions=[Decision(key=diff.key, strategy="keep", decided_by="default")],
    )
    text = to_yaml(decisions)
    assert from_yaml(text) == decisions


def test_from_yaml_invalid_yaml_raises_value_error():
    with pytest.raises(ValueError):
        from_yaml("::: not yaml :::\n  - [")


def test_from_yaml_wrong_shape_raises_validation_error():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        from_yaml("just: a string\n")


def test_decision_with_params_round_trips_through_yaml():
    diff = _diff(rule_id="PARAM-RULE")
    decisions = DecisionSet(
        differences_fingerprint=fingerprint([diff]),
        decisions=[
            Decision(
                key=diff.key,
                strategy="ranged",
                decided_by="user",
                params={"range": "10.80.0.0", "prefix_length": 24},
            )
        ],
    )
    text = to_yaml(decisions)
    assert "range" in text
    assert from_yaml(text) == decisions


def test_yaml_omits_empty_params():
    diff = _diff()
    decisions = DecisionSet(
        differences_fingerprint=fingerprint([diff]),
        decisions=[Decision(key=diff.key, strategy="keep", decided_by="default")],
    )
    assert "params" not in to_yaml(decisions)


def test_merge_decisions_fills_default_params_on_creation():
    rule = _param_rule()
    diff = _diff(rule_id="PARAM-RULE")
    merged, dropped = merge_decisions(None, [diff], (rule,))
    assert dropped == []
    assert merged.decisions[0].params == {"range": "10.0.0.0", "prefix_length": 24}


def test_merge_decisions_keeps_user_params_when_strategy_unchanged():
    rule = _param_rule()
    diff = _diff(rule_id="PARAM-RULE")
    existing = DecisionSet(
        differences_fingerprint="stale",
        decisions=[
            Decision(
                key=diff.key,
                strategy="ranged",
                decided_by="user",
                params={"range": "10.80.0.0", "prefix_length": 20},
            )
        ],
    )
    merged, dropped = merge_decisions(existing, [diff], (rule,))
    assert dropped == []
    assert merged.decisions[0].params == {"range": "10.80.0.0", "prefix_length": 20}


def test_merge_decisions_refills_params_when_strategy_changed_to_invalid_then_reset():
    # An invalid-strategy decision is treated as a fresh default (existing behaviour),
    # which also means it gets fresh default params rather than carrying over stale ones.
    rule = _param_rule()
    diff = _diff(rule_id="PARAM-RULE")
    existing = DecisionSet(
        differences_fingerprint="stale",
        decisions=[
            Decision(
                key=diff.key,
                strategy="no-such-strategy",
                decided_by="user",
                params={"range": "10.80.0.0", "prefix_length": 20},
            )
        ],
    )
    merged, dropped = merge_decisions(existing, [diff], (rule,))
    assert dropped == [diff.key]
    assert merged.decisions[0].strategy == "ranged"
    assert merged.decisions[0].params == {"range": "10.0.0.0", "prefix_length": 24}


def test_merge_decisions_resets_invalid_params_and_reports_dropped():
    rule = _param_rule()
    diff = _diff(rule_id="PARAM-RULE")
    existing = DecisionSet(
        differences_fingerprint="stale",
        decisions=[
            Decision(
                key=diff.key,
                strategy="ranged",
                decided_by="user",
                params={"range": "10.80.0.0", "prefix_length": 30},
            )
        ],
    )
    merged, dropped = merge_decisions(existing, [diff], (rule,))
    assert len(dropped) == 1
    assert dropped[0].startswith(f"{diff.key}: ")
    assert dropped[0].endswith("; reset to defaults")
    assert merged.decisions[0].params == {"range": "10.0.0.0", "prefix_length": 24}


def test_readiness_problems_reports_invalid_params():
    rule = _param_rule()
    diff = _diff(rule_id="PARAM-RULE")
    decisions = DecisionSet(
        differences_fingerprint=fingerprint([diff]),
        decisions=[
            Decision(
                key=diff.key,
                strategy="ranged",
                decided_by="user",
                params={"range": "10.80.0.0", "prefix_length": 30},
            )
        ],
    )
    problems = readiness_problems(decisions, [diff], (rule,))
    assert problems == ["PARAM-RULE@network:a: prefix_length must be between 16 and 24"]
