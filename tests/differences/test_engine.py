"""Tests for differences/engine.py (plan 3 task 1)."""

import pytest

from easymigrate.differences.engine import ResolvedIR, catalogue, detect, resolve_ir
from easymigrate.differences.model import (
    DecisionSet,
    Difference,
    DifferenceRule,
    DifferenceRuleError,
    Strategy,
)
from easymigrate.differences.rules.aws_gcp.network import NetIsolationVmLevel, NetZoneRegional
from easymigrate.ir.base import IRDocument, SourceRef
from easymigrate.ir.network import Network, Subnet

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


def _subnet(
    id_: str, network: str, cidr: str, *, zone: str, internet: str, map_public_ip: bool = False
) -> Subnet:
    return Subnet(
        id=id_,
        name=id_.split(":")[1],
        source=SourceRef(cloud="aws", type="subnet", id=id_, region="ap-south-1"),
        network=network,
        cidr=cidr,
        zone=zone,
        internet=internet,
        map_public_ip=map_public_ip,
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
        "apply": apply_fn or (lambda self, ir, diff, strategy_id, params=None: ir),
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

    def boom(self, ir, diff, strategy_id, params=None):
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

    result = resolve_ir(doc, [diff], decisions, rules=(rule,))

    assert isinstance(result, ResolvedIR)
    assert result.superseded == []
    resolved = result.document
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
    rule = _rule("TEST-RULE", apply_fn=lambda self, ir, diff, strategy_id, params=None: _doc())

    result = resolve_ir(doc, [diff], decisions, rules=(rule,))
    assert result.document.objects == []
    assert result.superseded == []


def test_resolve_ir_supersedes_decision_whose_subject_was_merged_away():
    """Real case: NET-ZONE-REGIONAL merges an isolated subnet away, so
    NET-ISOLATION-VM-LEVEL's decision on it becomes a silent no-op unless the engine
    detects and reports it."""
    from easymigrate.differences.engine import catalogue as _catalogue
    from easymigrate.differences.model import Decision

    network = _network("network:main", "10.0.0.0/16")
    public = _subnet(
        "subnet:pub", "network:main", "10.0.1.0/24", zone="ap-south-1a", internet="public"
    )
    isolated = _subnet(
        "subnet:app", "network:main", "10.0.2.0/24", zone="ap-south-1b", internet="isolated"
    )
    doc = _doc(network, public, isolated)

    rules = (NetZoneRegional(), NetIsolationVmLevel())
    assert [r.id for r in rules] == [r.id for r in _catalogue() if r.id in {r2.id for r2 in rules}]

    differences = detect(doc, rules=rules)
    zone_key = "NET-ZONE-REGIONAL@network:main"
    isolation_key = "NET-ISOLATION-VM-LEVEL@subnet:app"
    assert {d.key for d in differences} == {zone_key, isolation_key}

    decisions = DecisionSet(
        differences_fingerprint="x",
        decisions=[
            Decision(key=zone_key, strategy="merge_per_network", decided_by="user"),
            Decision(key=isolation_key, strategy="no_external_ip", decided_by="default"),
        ],
    )

    result = resolve_ir(doc, differences, decisions, rules=rules)

    assert len(result.superseded) == 1
    superseded = result.superseded[0]
    assert superseded.key == isolation_key
    assert superseded.strategy == "no_external_ip"
    assert superseded.subject == "subnet:app"
    assert superseded.superseded_by == zone_key
    assert "subnet:app" in superseded.message
    assert zone_key in superseded.message
    assert "merge_per_network" in superseded.message

    # the merge went ahead, and the document is still valid
    assert "subnet:app" not in {o.id for o in result.document.objects}
    assert IRDocument.model_validate_json(result.document.model_dump_json()) == result.document


def test_resolve_ir_supersedes_decision_whose_subject_was_excluded():
    """Fake-rule pair: an earlier rule excludes an object, and a later difference whose
    primary subject is that object is reported superseded rather than silently dropped."""
    from easymigrate.differences.model import Decision

    doc = _doc(_network())
    exclude_diff = Difference(
        key="EXCLUDE-RULE@network:a", rule_id="EXCLUDE-RULE", subjects=["network:a"], context={}
    )
    later_diff = Difference(
        key="LATER-RULE@network:a", rule_id="LATER-RULE", subjects=["network:a"], context={}
    )

    def exclude_apply(self, ir, diff, strategy_id, params=None):
        obj = ir.get("network:a")
        return ir.model_copy(
            update={
                "objects": [
                    obj.model_copy(update={"excluded": "excluded by EXCLUDE-RULE"})
                    if o.id == obj.id
                    else o
                    for o in ir.objects
                ]
            }
        )

    exclude_rule = _rule("EXCLUDE-RULE", apply_fn=exclude_apply)
    later_rule = _rule("LATER-RULE")

    decisions = DecisionSet(
        differences_fingerprint="x",
        decisions=[
            Decision(key=exclude_diff.key, strategy="keep", decided_by="default"),
            Decision(key=later_diff.key, strategy="keep", decided_by="default"),
        ],
    )

    result = resolve_ir(
        doc, [exclude_diff, later_diff], decisions, rules=(exclude_rule, later_rule)
    )

    assert len(result.superseded) == 1
    superseded = result.superseded[0]
    assert superseded.key == later_diff.key
    assert superseded.subject == "network:a"
    assert superseded.superseded_by == exclude_diff.key


def test_resolve_ir_missing_decision_raises():
    doc = _doc(_network())
    diff = Difference(
        key="TEST-RULE@network:a", rule_id="TEST-RULE", subjects=["network:a"], context={}
    )
    decisions = DecisionSet(differences_fingerprint="x", decisions=[])
    rule = _rule("TEST-RULE")

    with pytest.raises(ValueError, match="missing decision"):
        resolve_ir(doc, [diff], decisions, rules=(rule,))
