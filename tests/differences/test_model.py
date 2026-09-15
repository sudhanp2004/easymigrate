"""Tests for differences/model.py (plan 3 task 1)."""

import pytest

from easymigrate.differences.model import (
    Decision,
    DecisionSet,
    Difference,
    DifferenceRule,
    DifferenceRuleError,
    Strategy,
)
from easymigrate.ir.base import IRDocument

_KEEP = Strategy(id="keep", title="Keep", effect="no change", risk="none")
_MANUAL = Strategy(id="manual", title="Manual", effect="omit", risk="low", manual=True)


def _make_rule(**overrides):
    attrs = {
        "id": "TEST-RULE",
        "family": "test",
        "title": "Test rule",
        "impact": "test impact",
        "strategies": (_KEEP, _MANUAL),
        "default": "keep",
        "detect": lambda self, ir: [],
        "apply": lambda self, ir, diff, strategy_id: ir,
    }
    attrs.update(overrides)
    return type("_Rule", (DifferenceRule,), attrs)


def test_valid_rule_defines_cleanly():
    rule_cls = _make_rule()
    rule = rule_cls()
    assert rule.strategy("keep") is _KEEP
    with pytest.raises(KeyError):
        rule.strategy("nope")


def test_invalid_default_raises_at_class_definition():
    with pytest.raises(TypeError):
        _make_rule(default="does-not-exist")


def test_duplicate_strategy_ids_raise_at_class_definition():
    with pytest.raises(TypeError):
        _make_rule(strategies=(_KEEP, Strategy(id="keep", title="x", effect="y", risk="none")))


def test_bad_rule_id_raises_at_class_definition():
    with pytest.raises(TypeError):
        _make_rule(id="lowercase-bad")
    with pytest.raises(TypeError):
        _make_rule(id="NOHYPHEN")


def test_difference_builds_key_from_first_subject():
    rule_cls = _make_rule()
    rule = rule_cls()
    diff = rule.difference(["network:a", "subnet:b"], zones=["a", "b"])
    assert diff.key == "TEST-RULE@network:a"
    assert diff.rule_id == "TEST-RULE"
    assert diff.subjects == ["network:a", "subnet:b"]
    assert diff.context == {"zones": ["a", "b"]}


def test_difference_rule_error_message():
    cause = ValueError("boom")
    err = DifferenceRuleError("TEST-RULE", cause)
    assert str(err) == "difference rule TEST-RULE failed: boom"
    assert err.rule_id == "TEST-RULE"
    assert err.cause is cause


def test_decision_and_decision_set_round_trip():
    diffs = Difference(
        key="TEST-RULE@network:a", rule_id="TEST-RULE", subjects=["network:a"], context={}
    )
    decision = Decision(key=diffs.key, strategy="keep", decided_by="default")
    decision_set = DecisionSet(differences_fingerprint="abc", decisions=[decision])
    assert decision_set.schema_version == "1"
    assert decision_set.acknowledged_in_bulk is False


def test_difference_rule_is_abstract():
    with pytest.raises(TypeError):
        DifferenceRule()


def test_apply_and_detect_receive_ir_document():
    rule_cls = _make_rule()
    rule = rule_cls()
    doc = IRDocument(source={})
    assert rule.detect(doc) == []
    assert rule.apply(doc, rule.difference(["x:1"]), "keep") is doc
