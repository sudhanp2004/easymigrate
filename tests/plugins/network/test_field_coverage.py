"""No silent mapping (plan 4 A4): every IR field the generator could touch must be
declared in FIELD_COVERAGE, so adding a field without declaring it fails CI."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from easymigrate.differences.engine import catalogue
from easymigrate.ir.network import (
    AclRule,
    CidrPeer,
    CustomRoute,
    ExternalPolicyPeer,
    FirewallPolicy,
    FirewallRule,
    Nat,
    Network,
    NetworkAcl,
    PolicyPeer,
    PrefixListPeer,
    SelfPeer,
    Subnet,
)
from easymigrate.plugins.network.generate_gcp import FIELD_COVERAGE

_KINDS = {
    "network": Network,
    "subnet": Subnet,
    "nat": Nat,
    "firewall_policy": FirewallPolicy,
    "network_acl": NetworkAcl,
    "custom_route": CustomRoute,
}

_NESTED = {
    "FirewallRule": FirewallRule,
    "AclRule": AclRule,
    "CidrPeer": CidrPeer,
    "PolicyPeer": PolicyPeer,
    "ExternalPolicyPeer": ExternalPolicyPeer,
    "SelfPeer": SelfPeer,
    "PrefixListPeer": PrefixListPeer,
}

_VALID_PREFIXES = ("exact", "metadata", "rule:", "hint:", "not_generated:", "derived:")

_RULES_SRC = Path(__file__).resolve().parents[3] / "src" / "easymigrate" / "differences" / "rules"


@pytest.mark.parametrize("kind_name, model", sorted(_KINDS.items()))
def test_every_ir_field_is_declared(kind_name, model):
    declared = set(FIELD_COVERAGE[kind_name])
    assert declared == set(model.model_fields)


@pytest.mark.parametrize("name, model", sorted(_NESTED.items()))
def test_every_nested_field_is_declared(name, model):
    declared = set(FIELD_COVERAGE[name])
    assert declared == set(model.model_fields)


def test_every_kind_and_nested_model_has_coverage():
    assert set(FIELD_COVERAGE) == set(_KINDS) | set(_NESTED)


def _all_declarations():
    for kind, fields in FIELD_COVERAGE.items():
        for field, declaration in fields.items():
            yield kind, field, declaration


@pytest.mark.parametrize("kind, field, declaration", list(_all_declarations()))
def test_declarations_use_an_allowed_value(kind, field, declaration):
    assert (
        declaration == "exact"
        or declaration == "metadata"
        or (declaration.startswith(("rule:", "hint:", "not_generated:", "derived:")))
    )


def test_every_rule_id_exists_in_catalogue():
    known = {rule.id for rule in catalogue()}
    for _kind, _field, declaration in _all_declarations():
        if declaration.startswith("rule:"):
            rule_id = declaration.removeprefix("rule:")
            assert rule_id in known, f"unknown rule id {rule_id!r} in FIELD_COVERAGE"


def test_every_hint_key_is_produced_by_a_rule():
    source = "\n".join(p.read_text() for p in _RULES_SRC.rglob("*.py"))
    for _kind, _field, declaration in _all_declarations():
        if declaration.startswith("hint:"):
            key = declaration.removeprefix("hint:")
            pattern = re.compile(rf'["\']{re.escape(key)}["\']')
            assert pattern.search(source), f"hint key {key!r} is not produced by any rule"
