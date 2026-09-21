"""Golden regression tests: hand-built snapshots → expected IR (spec 4 §4, plan 1 §Tasks).

Regenerate snapshots with `uv run python scripts/make_compute_fixtures.py`, then expected files
with `uv run pytest --update-golden tests/plugins/compute/test_golden.py`, and review the diff.

Each fixture runs the whole scan→IR path: the network plugin normalizes first (as
`ComputePlugin.requires` demands, spec 4 §2.2), and the compute normalizer resolves subnet and
security-group native ids through the resulting `ctx.objects`, exactly as `pipeline.analyze`
would.

Plans 3-5 add `VM-*` difference keys and golden HCL against these same case names.
"""

from collections.abc import Callable
from pathlib import Path

import pytest

from easymigrate.core.plugin import NormalizeContext, Snapshot
from easymigrate.ir import IRDocument
from easymigrate.plugins.compute.normalize import normalize as normalize_compute
from easymigrate.plugins.network.normalize import normalize as normalize_network

FIXTURES = Path(__file__).parent / "fixtures"
CASES = sorted(p.name for p in FIXTURES.glob("*") if (p / "snapshot").is_dir())


def _kinds(doc: IRDocument, kind: str) -> list:
    return [o for o in doc.objects if o.kind == kind]


def _codes(doc: IRDocument) -> set[str]:
    return {f.code for f in doc.findings}


# One key fact per case, so a wrong golden file cannot silently pass review.
KEY_FACTS: dict[str, Callable[[IRDocument], bool]] = {
    "standalone_linux": lambda doc: (
        _kinds(doc, "compute_instance")[0].external_ip == "external_address:web-eip"
        and [a.attached for a in _kinds(doc, "external_address")] == [False, True]
        and _kinds(doc, "compute_instance")[0].key_material is not None
        and _kinds(doc, "compute_instance")[0].user_data == "#!/bin/bash\necho hello\n"
    ),
    "stopped_instance": lambda doc: _kinds(doc, "compute_instance")[0].state == "stopped",
    "extra_volumes": lambda doc: (
        len(_kinds(doc, "disk")) == 3
        and any(d.instance_store for d in _kinds(doc, "disk"))
        and any(d.iops == 6000 and d.throughput_mibps == 250 for d in _kinds(doc, "disk"))
    ),
    "windows_instance": lambda doc: _kinds(doc, "compute_instance")[0].platform == "windows",
    "small_boot_disk": lambda doc: _kinds(doc, "disk")[0].size_gib == 8,
    "spot_instance": lambda doc: (
        _kinds(doc, "compute_instance")[0].spot
        and _kinds(doc, "compute_instance")[0].tenancy == "dedicated"
        and _kinds(doc, "compute_instance")[0].iam_profile is not None
    ),
    "eks_member": lambda doc: (
        _kinds(doc, "compute_instance")[0].eks_cluster == "test-eks-cluster"
        and "VM-EKS-MEMBER" in _codes(doc)
    ),
    "asg_two_az": lambda doc: (
        len(_kinds(doc, "instance_group")) == 1
        and len(_kinds(doc, "instance_template")) == 1
        and _kinds(doc, "instance_group")[0].scaling_policies[0].target_value == 60.0
        and "VM-LT-VERSION-UNUSED" in _codes(doc)
    ),
    "unsupported_resources": lambda doc: (
        _kinds(doc, "compute_instance") == []
        and len([f for f in doc.findings if f.level == "unsupported"]) == 2
    ),
}


def _normalize(case: str) -> IRDocument:
    ctx = NormalizeContext(account_id="123456789012", region="ap-south-1", strict=True)
    snapshot = Snapshot.from_dir(FIXTURES / case / "snapshot")
    network_objects = normalize_network(snapshot, ctx)
    ctx.objects.update({obj.id: obj for obj in network_objects})
    compute_objects = normalize_compute(snapshot, ctx)
    return IRDocument(
        source={"cloud": "aws", "account_id": "123456789012", "region": "ap-south-1"},
        objects=[*network_objects, *compute_objects],
        findings=ctx.findings,
    )


def test_every_spec_case_has_a_fixture():
    assert set(CASES) == set(KEY_FACTS)
    assert CASES == [
        "asg_two_az",
        "eks_member",
        "extra_volumes",
        "small_boot_disk",
        "spot_instance",
        "standalone_linux",
        "stopped_instance",
        "unsupported_resources",
        "windows_instance",
    ]


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


@pytest.mark.parametrize("case", CASES)
def test_every_fixture_produces_a_loadable_document(case):
    """Integrity is the whole point of forward references (spec 4 §2.1): every subnet,
    policy, disk and address an instance names must exist in the same document."""
    doc = _normalize(case)
    IRDocument.model_validate_json(doc.model_dump_json())


@pytest.mark.parametrize("case", CASES)
def test_normalization_is_deterministic(case):
    first = _normalize(case).model_dump_json()
    second = _normalize(case).model_dump_json()
    assert first == second


def test_applies_to_no_longer_exists_on_firewall_policy():
    """Spec 4 §2.1: the back-reference is deleted; membership is derived from forward
    references by `differences.membership.policy_members`."""
    from easymigrate.ir.network import FirewallPolicy

    assert "applies_to" not in FirewallPolicy.model_fields
