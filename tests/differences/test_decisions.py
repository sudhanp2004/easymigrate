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
