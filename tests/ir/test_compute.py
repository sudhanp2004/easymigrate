"""Compute intent objects (spec 4 §4.2) and the forward-reference claim of spec 4 §2.1."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from easymigrate.ir.base import IRDocument
from easymigrate.ir.compute import (
    ComputeInstance,
    Disk,
    ExternalAddress,
    ImdsOptions,
    InstanceGroup,
    InstanceTemplate,
    ScalingPolicy,
)
from easymigrate.ir.network import Network, Subnet


def source(type_: str, id_: str) -> dict[str, str]:
    return {"cloud": "aws", "type": type_, "id": id_, "region": "ap-south-1"}


def disk(id_: str = "disk:boot", **overrides: object) -> Disk:
    fields: dict[str, object] = {
        "id": id_,
        "name": id_.split(":", 1)[1],
        "source": source("volume", "vol-1"),
        "size_gib": 20,
        "type": "gp3",
        "zone": "ap-south-1a",
        "boot": True,
    }
    fields.update(overrides)
    return Disk(**fields)  # type: ignore[arg-type]


def instance(**overrides: object) -> ComputeInstance:
    fields: dict[str, object] = {
        "id": "compute_instance:web",
        "name": "web",
        "source": source("instance", "i-1"),
        "instance_type": "t3.micro",
        "architecture": "x86_64",
        "state": "running",
        "zone": "ap-south-1a",
        "subnet": "subnet:public-1",
        "image": "ami-1",
        "platform": "linux",
    }
    fields.update(overrides)
    return ComputeInstance(**fields)  # type: ignore[arg-type]


def subnet() -> Subnet:
    return Subnet(
        id="subnet:public-1",
        name="public-1",
        source=source("subnet", "subnet-1"),
        network="network:vpc",
        cidr="10.0.1.0/24",
        zone="ap-south-1a",
        internet="public",
        map_public_ip=True,
    )


def network() -> Network:
    return Network(
        id="network:vpc",
        name="vpc",
        source=source("vpc", "vpc-1"),
        cidr_blocks=["10.0.0.0/16"],
        dns_hostnames=True,
        dns_resolution=True,
    )


def test_minimal_instance_round_trips_through_document():
    doc = IRDocument(
        source={"cloud": "aws", "account_id": "1", "region": "ap-south-1"},
        objects=[network(), subnet(), instance()],
    )
    loaded = IRDocument.model_validate_json(doc.model_dump_json())
    assert isinstance(loaded.get("compute_instance:web"), ComputeInstance)


def test_references_cover_subnet_policies_and_disks():
    obj = instance(
        firewall_policies=["firewall_policy:web-sg"],
        disks=["disk:boot", "disk:data"],
    )
    assert obj.references() == [
        "subnet:public-1",
        "firewall_policy:web-sg",
        "disk:boot",
        "disk:data",
    ]


@pytest.mark.parametrize("value", ["none", "ephemeral"])
def test_references_omit_non_id_external_ip(value: str):
    assert instance(external_ip=value).references() == ["subnet:public-1"]


def test_references_include_external_address_id():
    obj = instance(external_ip="external_address:eip-1")
    assert "external_address:eip-1" in obj.references()


def test_external_ip_rejects_anything_else():
    with pytest.raises(ValidationError, match="external_ip"):
        instance(external_ip="public")


def test_instance_store_disk_forbids_type_and_iops():
    with pytest.raises(ValidationError, match="instance_store"):
        disk(instance_store=True, type="gp3", boot=False)
    with pytest.raises(ValidationError, match="instance_store"):
        disk(instance_store=True, type=None, iops=100, boot=False)


def test_non_instance_store_disk_requires_a_type():
    with pytest.raises(ValidationError, match="type"):
        disk(type=None)


def test_instance_store_disk_is_valid_without_type_or_size():
    obj = disk(id_="disk:scratch", instance_store=True, type=None, size_gib=None, boot=False)
    assert obj.instance_store is True


@pytest.mark.parametrize("disk_type", ["gp3", "io1", "io2"])
def test_iops_allowed_on_provisionable_types(disk_type: str):
    assert disk(type=disk_type, iops=3000).iops == 3000


def test_iops_rejected_on_gp2():
    with pytest.raises(ValidationError, match="iops"):
        disk(type="gp2", iops=3000)


def test_throughput_rejected_on_io1():
    with pytest.raises(ValidationError, match="throughput_mibps"):
        disk(type="io1", throughput_mibps=250)


def group(**overrides: object) -> InstanceGroup:
    fields: dict[str, object] = {
        "id": "instance_group:web-asg",
        "name": "web-asg",
        "source": source("autoScalingGroup", "web-asg"),
        "template": "instance_template:web-lt",
        "min_size": 1,
        "max_size": 3,
        "desired_capacity": 2,
        "zones": ["ap-south-1a"],
    }
    fields.update(overrides)
    return InstanceGroup(**fields)  # type: ignore[arg-type]


def test_instance_group_rejects_desired_above_max():
    with pytest.raises(ValidationError, match="desired_capacity"):
        group(desired_capacity=5)


def test_instance_group_rejects_min_above_desired():
    with pytest.raises(ValidationError, match="desired_capacity"):
        group(min_size=3, desired_capacity=2, max_size=4)


def test_instance_group_references_template_and_subnets():
    obj = group(subnets=["subnet:public-1", "subnet:public-2"])
    assert obj.references() == [
        "instance_template:web-lt",
        "subnet:public-1",
        "subnet:public-2",
    ]


def test_imds_hop_limit_must_be_positive():
    with pytest.raises(ValidationError):
        ImdsOptions(http_tokens="required", hop_limit=0)


def test_scaling_policy_accepts_a_target_tracking_shape():
    policy = ScalingPolicy(
        name="cpu",
        policy_type="TargetTrackingScaling",
        metric="ASGAverageCPUUtilization",
        target_value=60.0,
    )
    assert policy.target_value == 60.0


def test_unknown_field_is_rejected():
    with pytest.raises(ValidationError):
        instance(nonsense=True)


def test_template_references_policies_and_disks():
    template = InstanceTemplate(
        id="instance_template:web-lt",
        name="web-lt",
        source=source("launch-template", "lt-1"),
        instance_type="t3.micro",
        architecture="x86_64",
        image="ami-1",
        platform="linux",
        version="3",
        default_version="1",
        firewall_policies=["firewall_policy:web-sg"],
        disks=["disk:boot"],
    )
    assert template.references() == ["firewall_policy:web-sg", "disk:boot"]


def test_external_address_has_no_references():
    address = ExternalAddress(
        id="external_address:eip-1",
        name="eip-1",
        source=source("elastic-ip", "eipalloc-1"),
        address="13.1.1.1",
        attached=True,
        scope="vpc",
    )
    assert address.references() == []


def _document(objects: list[object]) -> str:
    return json.dumps(
        {
            "schema_version": "1",
            "source": {"cloud": "aws", "account_id": "1", "region": "ap-south-1"},
            "objects": [obj.model_dump(mode="json") for obj in objects],  # type: ignore[attr-defined]
        }
    )


@pytest.mark.parametrize(
    "overrides, missing",
    [
        ({"subnet": "subnet:absent"}, "subnet:absent"),
        ({"firewall_policies": ["firewall_policy:absent"]}, "firewall_policy:absent"),
        ({"disks": ["disk:absent"]}, "disk:absent"),
        ({"external_ip": "external_address:absent"}, "external_address:absent"),
    ],
)
def test_missing_forward_reference_fails_integrity(overrides: dict, missing: str):
    """Spec 4 §2.1: forward references are validated; a back-reference could not be.

    Everything the instance points at is present except the one reference under test, so the
    failure names that reference rather than an unrelated missing object.
    """
    obj = instance(**overrides)
    with pytest.raises(ValueError, match=missing):
        IRDocument.model_validate_json(_document([network(), subnet(), obj]))
