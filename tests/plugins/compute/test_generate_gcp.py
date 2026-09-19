"""Compute → GCP Terraform generation (spec 4 §6)."""

from __future__ import annotations

import pytest

from easymigrate.core.plugin import GenerateContext, GenerationError
from easymigrate.ir.base import IRDocument, SourceRef
from easymigrate.ir.compute import ComputeInstance, Disk
from easymigrate.ir.network import FirewallPolicy, Network, Subnet
from easymigrate.plugins.compute.generate_gcp import FIELD_COVERAGE, generate


def _source(type_: str, native: str) -> SourceRef:
    return SourceRef(cloud="aws", type=type_, id=native, region="ap-south-1")


def _ctx() -> GenerateContext:
    return GenerateContext(project="p", region="asia-south1", version="0.1.0")


def _network():
    return Network(
        id="network:main",
        name="main",
        source=_source("vpc", "vpc-1"),
        cidr_blocks=["10.0.0.0/16"],
        dns_hostnames=True,
        dns_resolution=True,
    )


def _subnet(id_="subnet:a"):
    return Subnet(
        id=id_,
        name=id_.split(":", 1)[1],
        source=_source("subnet", id_),
        network="network:main",
        cidr="10.0.1.0/24",
        zone="ap-south-1a",
        internet="public",
        map_public_ip=False,
    )


def _policy(id_="firewall_policy:web-sg"):
    return FirewallPolicy(
        id=id_,
        name=id_.split(":", 1)[1],
        source=_source("security-group", id_),
        network="network:main",
    )


def _disk(id_="disk:boot", boot=True, instance_store=False, **kw):
    hints = kw.pop("target_hints", {})
    return Disk(
        id=id_,
        name=id_.split(":", 1)[1],
        source=_source("volume", id_),
        size_gib=kw.pop("size_gib", 20),
        type=None if instance_store else kw.pop("type", "gp3"),
        zone="ap-south-1a",
        boot=boot,
        instance_store=instance_store,
        target_hints=hints,
        **kw,
    )


def _instance(disks=("disk:boot",), policies=(), **overrides):
    hints = {
        "machine_type": "n2-standard-4",
        "boot_image": {"project": "rocky-linux-cloud", "family": "rocky-linux-9"},
        "zone_letter": "a",
    }
    hints.update(overrides.pop("target_hints", {}))
    return ComputeInstance(
        id=overrides.pop("id", "compute_instance:web"),
        name="web",
        source=_source("instance", "i-1"),
        instance_type="m5.xlarge",
        vcpus=4,
        memory_mib=16384,
        architecture="x86_64",
        state=overrides.pop("state", "running"),
        zone="ap-south-1a",
        subnet="subnet:a",
        image="ami-1",
        platform="linux",
        disks=list(disks),
        firewall_policies=list(policies),
        target_hints=hints,
        **overrides,
    )


def _doc(*objects) -> IRDocument:
    return IRDocument(source={"cloud": "aws"}, objects=list(objects))


def _render(*objects) -> str:
    return generate(_doc(*objects), _ctx()).render()


def test_minimal_instance_renders():
    out = _render(_network(), _subnet(), _disk(), _instance())

    assert 'resource "google_compute_instance" "web"' in out
    assert 'machine_type = "n2-standard-4"' in out
    assert 'zone          = "${var.region}-a"' in out or "${var.region}-a" in out
    assert "rocky-linux-cloud/rocky-linux-9" in out
    assert "google_compute_subnetwork.a.id" in out


def test_tags_match_the_network_generators_own_tag_for_that_policy():
    """A rename on either side must fail this, so the tag is taken from the shared
    allocator rather than hard-coded here (spec 4 §6.2)."""
    from easymigrate.plugins.network.generate_gcp import policy_network_tags

    policy = _policy()
    expected, _ = policy_network_tags([policy])

    out = _render(_network(), _subnet(), policy, _disk(), _instance(policies=[policy.id]))

    assert expected[policy.id] in out


def test_instance_with_no_surviving_policies_emits_no_tags_and_a_finding():
    policy = _policy()
    dropped = policy.model_copy(update={"excluded": "DB-SG-FIREWALL: dropped"})
    ctx = _ctx()

    out = generate(
        _doc(_network(), _subnet(), dropped, _disk(), _instance(policies=[policy.id])), ctx
    ).render()

    assert "tags" not in out
    assert [f.code for f in ctx.findings] == ["GEN-NO-TAGS"]


def test_private_instance_has_no_access_config():
    out = _render(_network(), _subnet(), _disk(), _instance())
    assert "access_config" not in out


def test_external_instance_gets_an_access_config():
    out = _render(_network(), _subnet(), _disk(), _instance(target_hints={"external": "ephemeral"}))
    assert "access_config" in out


def test_stopped_instance_renders_desired_status():
    out = _render(
        _network(),
        _subnet(),
        _disk(),
        _instance(state="stopped", target_hints={"desired_status": "TERMINATED"}),
    )
    assert 'desired_status = "TERMINATED"' in out


def test_extra_disk_renders_a_disk_resource_and_an_attachment():
    out = _render(
        _network(),
        _subnet(),
        _disk(),
        _disk(id_="disk:data", boot=False, size_gib=500, target_hints={"disk_type": "pd-ssd"}),
        _instance(disks=["disk:boot", "disk:data"]),
    )

    assert 'resource "google_compute_disk" "data"' in out
    assert 'type = "pd-ssd"' in out
    assert "google_compute_disk.data.id" in out


def test_excluded_disk_renders_neither_resource_nor_attachment():
    excluded = _disk(id_="disk:data", boot=False).model_copy(
        update={"excluded": "VM-DISK-EMPTY: not created"}
    )
    out = _render(
        _network(), _subnet(), _disk(), excluded, _instance(disks=["disk:boot", "disk:data"])
    )

    assert "google_compute_disk" not in out
    assert "attached_disk" not in out


def test_instance_store_renders_scratch_disk_not_a_persistent_disk():
    out = _render(
        _network(),
        _subnet(),
        _disk(),
        _disk(id_="disk:scratch", boot=False, instance_store=True, size_gib=None),
        _instance(disks=["disk:boot", "disk:scratch"]),
    )

    assert "scratch_disk" in out
    assert "google_compute_disk" not in out


def test_eks_members_are_not_generated():
    out = _render(_network(), _subnet(), _disk(), _instance(eks_cluster="test-eks-cluster"))
    assert out.strip() == "" or "google_compute_instance" not in out


@pytest.mark.parametrize("hint", ["machine_type", "boot_image", "zone_letter"])
def test_missing_hint_is_a_generation_error(hint: str):
    """A missing hint means a difference rule did not run; generation refuses rather than
    inventing a value (spec 2 §6's contract, kept)."""
    instance = _instance()
    hints = dict(instance.target_hints)
    hints.pop(hint)
    broken = instance.model_copy(update={"target_hints": hints})

    with pytest.raises(GenerationError, match=hint):
        _render(_network(), _subnet(), _disk(), broken)


def test_missing_boot_disk_is_a_generation_error():
    with pytest.raises(GenerationError, match="boot disk"):
        _render(_network(), _subnet(), _instance(disks=[]))


def test_excluded_boot_disk_is_a_generation_error():
    boot = _disk().model_copy(update={"excluded": "VM-DISK-TYPE: handled manually"})
    with pytest.raises(GenerationError, match="boot disk"):
        _render(_network(), _subnet(), boot, _instance())


def test_field_coverage_declares_every_field_of_every_compute_kind():
    from easymigrate.ir.compute import ExternalAddress, InstanceGroup, InstanceTemplate

    kinds = {
        "compute_instance": ComputeInstance,
        "disk": Disk,
        "external_address": ExternalAddress,
        "instance_template": InstanceTemplate,
        "instance_group": InstanceGroup,
    }
    assert set(FIELD_COVERAGE) == set(kinds)
    for kind, model in kinds.items():
        declared = set(FIELD_COVERAGE[kind])
        actual = set(model.model_fields)
        assert declared == actual, f"{kind}: {declared ^ actual}"


def test_generation_is_deterministic():
    objects = (
        _network(),
        _subnet(),
        _policy(),
        _disk(),
        _disk(id_="disk:data", boot=False),
        _instance(disks=["disk:boot", "disk:data"], policies=["firewall_policy:web-sg"]),
    )
    assert _render(*objects) == _render(*objects)
