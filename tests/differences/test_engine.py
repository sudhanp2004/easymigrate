"""Tests for differences/engine.py (plan 3 task 1)."""

import pytest

from easymigrate.differences.engine import catalogue, detect, resolve_ir
from easymigrate.differences.model import (
    DecisionSet,
    Difference,
    DifferenceRule,
    DifferenceRuleError,
    Strategy,
)
from easymigrate.ir.base import IRDocument, SourceRef
from easymigrate.ir.network import Network

_KEEP = Strategy(id="keep", title="Keep", effect="no change", risk="none")


def _network(id_="network:a", cidr="10.0.0.0/16") -> Network:
    return Network(
        id=id_,
        name=id_.split(":")[1],
        source=SourceRef(cloud="aws", type="vpc", id=id_, region="ap-south-1"),
        cidr_blocks=[cidr],
        dns_hostnames=True,
        dns_resolution=True,
    )


def _doc(*objects) -> IRDocument:
    return IRDocument(source={"cloud": "aws"}, objects=list(objects))


def _rule(rule_id, detect_fn=None, apply_fn=None, strategies=(_KEEP,), default="keep"):
    attrs = {
        "id": rule_id,
        "family": "test",
        "title": rule_id,
        "impact": "impact",
        "strategies": strategies,
        "default": default,
        "detect": detect_fn or (lambda self, ir: []),
        "apply": apply_fn or (lambda self, ir, diff, strategy_id: ir),
    }
    return type(f"_Rule_{rule_id.replace('-', '_')}", (DifferenceRule,), attrs)()


def test_catalogue_returns_aws_gcp_rules():
    from easymigrate.differences.rules.aws_gcp import AWS_GCP_RULES

    assert catalogue() == AWS_GCP_RULES


def test_detect_sorts_by_key():
    net_b = _network("network:b")
    net_a = _network("network:a")
    doc = _doc(net_a, net_b)
    rule = _rule(
        "TEST-RULE",
        detect_fn=lambda self, ir: [
            self.difference([o.id]) for o in sorted(ir.objects, key=lambda o: o.id, reverse=True)
        ],
    )
    differences = detect(doc, rules=(rule,))
    assert [d.key for d in differences] == ["TEST-RULE@network:a", "TEST-RULE@network:b"]


def test_detect_duplicate_key_raises():
    doc = _doc(_network())
    rule = _rule(
        "TEST-RULE",
        detect_fn=lambda self, ir: [self.difference(["network:a"]), self.difference(["network:a"])],
    )
    with pytest.raises(ValueError, match="duplicate difference key"):
        detect(doc, rules=(rule,))


def test_detect_failing_rule_raises_difference_rule_error():
    doc = _doc(_network())

    def boom(self, ir):
        raise RuntimeError("kaboom")

    rule = _rule("TEST-RULE", detect_fn=boom)
    with pytest.raises(DifferenceRuleError) as exc_info:
        detect(doc, rules=(rule,))
    assert exc_info.value.rule_id == "TEST-RULE"


def test_apply_failing_rule_raises_difference_rule_error():
    doc = _doc(_network())
    diff = Difference(
        key="TEST-RULE@network:a", rule_id="TEST-RULE", subjects=["network:a"], context={}
    )
    decisions = DecisionSet(differences_fingerprint="x", decisions=[])
    from easymigrate.differences.model import Decision

    decisions = decisions.model_copy(
        update={"decisions": [Decision(key=diff.key, strategy="keep", decided_by="default")]}
    )

    def boom(self, ir, diff, strategy_id):
        raise RuntimeError("kaboom")

    rule = _rule("TEST-RULE", apply_fn=boom)
    with pytest.raises(DifferenceRuleError) as exc_info:
        resolve_ir(doc, [diff], decisions, rules=(rule,))
    assert exc_info.value.rule_id == "TEST-RULE"


def test_resolve_ir_adds_resolution_ref_and_validates():
    doc = _doc(_network())
    diff = Difference(
        key="TEST-RULE@network:a", rule_id="TEST-RULE", subjects=["network:a"], context={}
    )
    from easymigrate.differences.model import Decision

    decisions = DecisionSet(
        differences_fingerprint="x",
        decisions=[Decision(key=diff.key, strategy="keep", decided_by="default")],
    )
    rule = _rule("TEST-RULE")

    resolved = resolve_ir(doc, [diff], decisions, rules=(rule,))

    obj = resolved.get("network:a")
    assert [r.model_dump() for r in obj.resolution] == [{"key": diff.key, "strategy": "keep"}]
    # round-trips through validation without error
    assert IRDocument.model_validate_json(resolved.model_dump_json()) == resolved


def test_resolve_ir_skips_removed_subject_without_error():
    doc = _doc(_network())
    diff = Difference(
        key="TEST-RULE@network:a", rule_id="TEST-RULE", subjects=["network:a"], context={}
    )
    from easymigrate.differences.model import Decision

    decisions = DecisionSet(
        differences_fingerprint="x",
        decisions=[Decision(key=diff.key, strategy="keep", decided_by="default")],
    )
    rule = _rule("TEST-RULE", apply_fn=lambda self, ir, diff, strategy_id: _doc())

    resolved = resolve_ir(doc, [diff], decisions, rules=(rule,))
    assert resolved.objects == []


def test_resolve_ir_missing_decision_raises():
    doc = _doc(_network())
    diff = Difference(
        key="TEST-RULE@network:a", rule_id="TEST-RULE", subjects=["network:a"], context={}
    )
    decisions = DecisionSet(differences_fingerprint="x", decisions=[])
    rule = _rule("TEST-RULE")

    with pytest.raises(ValueError, match="missing decision"):
        resolve_ir(doc, [diff], decisions, rules=(rule,))
