"""Golden regression tests: anonymized snapshots → expected IR (spec §7).

Regenerate snapshots with `uv run python scripts/make_network_fixtures.py`, then expected files
with `uv run pytest --update-golden tests/plugins/network/test_golden.py`, and review the diff."""

from collections.abc import Callable
from pathlib import Path

import pytest

from easymigrate.core.plugin import NormalizeContext, Snapshot
from easymigrate.ir import IRDocument
from easymigrate.ir.network import ExternalPolicyPeer, PolicyPeer
from easymigrate.plugins.network import NetworkPlugin

FIXTURES = Path(__file__).parent / "fixtures"
CASES = sorted(p.name for p in FIXTURES.glob("*") if (p / "snapshot").is_dir())


def _kinds(doc: IRDocument, kind: str) -> list:
    return [o for o in doc.objects if o.kind == kind]


def _peers(doc: IRDocument) -> list:
    return [rule.peer for policy in _kinds(doc, "firewall_policy") for rule in policy.rules]


# One key fact per case, so a wrong golden file cannot silently pass review.
KEY_FACTS: dict[str, Callable[[IRDocument], bool]] = {
    "basic_public": lambda doc: {s.internet for s in _kinds(doc, "subnet")} == {"public"},
    "private_with_nat": lambda doc: _kinds(doc, "nat")[0].serves_subnets == ["subnet:private"],
    "sg_references_sg": lambda doc: any(isinstance(p, PolicyPeer) for p in _peers(doc)),
    "custom_nacl": lambda doc: _kinds(doc, "network_acl")[0].subnets == ["subnet:db"],
    "peering_route": lambda doc: _kinds(doc, "custom_route")[0].target_type == "peering",
    "multi_vpc": lambda doc: len(_kinds(doc, "network")) == 2,
    "ipv6": lambda doc: (
        bool(_kinds(doc, "network")[0].ipv6_cidr_blocks)
        and _kinds(doc, "subnet")[0].ipv6_cidr is not None
    ),
    "external_sg_ref": lambda doc: any(isinstance(p, ExternalPolicyPeer) for p in _peers(doc)),
}


def _normalize(case: str) -> IRDocument:
    ctx = NormalizeContext(account_id="123456789012", region="ap-south-1", strict=True)
    objects = NetworkPlugin().normalize(Snapshot.from_dir(FIXTURES / case / "snapshot"), ctx)
    return IRDocument(
        source={"cloud": "aws", "account_id": "123456789012", "region": "ap-south-1"},
        objects=objects,
        findings=ctx.findings,
    )


def test_every_spec_case_has_a_fixture():
    assert set(CASES) == set(KEY_FACTS)


@pytest.mark.parametrize("case", CASES)
def test_golden(case, update_golden):
    doc = _normalize(case)
    actual = doc.model_dump_json(indent=2) + "\n"
    expected_path = FIXTURES / case / "expected_ir.json"

    if update_golden:
        expected_path.write_text(actual)

    assert expected_path.exists(), "missing golden file; run pytest --update-golden"
    assert actual == expected_path.read_text()
    assert KEY_FACTS[case](doc), f"key fact for {case} does not hold"
