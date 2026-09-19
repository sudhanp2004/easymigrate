"""AWS→GCP compute difference rules (spec 4 §5)."""

from __future__ import annotations

import pytest

from easymigrate.differences.rules.aws_gcp.compute import (
    VmBootImage,
    VmMachineType,
    VmZoneMapping,
)
from easymigrate.ir.base import IRDocument, SourceRef
from easymigrate.ir.compute import ComputeInstance, InstanceTemplate
from easymigrate.ir.network import Network, Subnet


def _source(type_: str, native: str) -> SourceRef:
    return SourceRef(cloud="aws", type=type_, id=native, region="ap-south-1")


def _network():
    return Network(
        id="network:main",
        name="main",
        source=_source("vpc", "vpc-1"),
        cidr_blocks=["10.0.0.0/16"],
        dns_hostnames=True,
        dns_resolution=True,
    )


def _subnet(id_="subnet:a", cidr="10.0.1.0/24"):
    return Subnet(
        id=id_,
        name=id_.split(":", 1)[1],
        source=_source("subnet", id_),
        network="network:main",
        cidr=cidr,
        zone="ap-south-1a",
        internet="public",
        map_public_ip=False,
    )


def _instance(
    id_="compute_instance:web",
    instance_type="m5.xlarge",
    vcpus=4,
    memory_mib=16384,
    platform="linux",
    platform_details="Linux/UNIX",
    zone="ap-south-1a",
    zone_id="aps1-az1",
    state="running",
):
    return ComputeInstance(
        id=id_,
        name=id_.split(":", 1)[1],
        source=_source("instance", id_),
        instance_type=instance_type,
        vcpus=vcpus,
        memory_mib=memory_mib,
        architecture="x86_64",
        state=state,
        zone=zone,
        zone_id=zone_id,
        subnet="subnet:a",
        image="ami-1",
        platform=platform,
        platform_details=platform_details,
    )


def _template(id_="instance_template:web-lt", instance_type="t3.small", vcpus=2, memory_mib=2048):
    return InstanceTemplate(
        id=id_,
        name=id_.split(":", 1)[1],
        source=_source("launch-template", id_),
        instance_type=instance_type,
        vcpus=vcpus,
        memory_mib=memory_mib,
        architecture="x86_64",
        image="ami-1",
        platform="linux",
        platform_details="Linux/UNIX",
        version="1",
    )


def _doc(*objects) -> IRDocument:
    return IRDocument(source={"cloud": "aws"}, objects=[_network(), _subnet(), *objects])


# ---- VM-BOOT-IMAGE ---------------------------------------------------------


def test_boot_image_is_high_risk_and_says_the_disk_did_not_come():
    strategy = VmBootImage().strategy("stock_image")
    assert strategy.risk == "high"
    assert "absent" in strategy.effect


def test_boot_image_fires_for_instances_and_templates():
    doc = _doc(_instance(), _template())
    diffs = VmBootImage().detect(doc)
    assert sorted(d.subjects[0] for d in diffs) == [
        "compute_instance:web",
        "instance_template:web-lt",
    ]


def test_amazon_linux_is_flagged_as_having_no_equivalent():
    doc = _doc(_instance(platform_details="Linux/UNIX"))
    diff = VmBootImage().detect(doc)[0]
    assert diff.context["no_equivalent"] is True
    assert diff.context["image_family"] == "rocky-linux-9"


def test_ubuntu_is_not_flagged_as_having_no_equivalent():
    doc = _doc(_instance(platform_details="Ubuntu Pro"))
    diff = VmBootImage().detect(doc)[0]
    assert diff.context["no_equivalent"] is False


def test_boot_image_apply_sets_the_hint():
    doc = _doc(_instance())
    rule = VmBootImage()
    diff = rule.detect(doc)[0]

    result = rule.apply(doc, diff, "stock_image")

    assert result.get("compute_instance:web").target_hints["boot_image"] == {
        "project": "rocky-linux-cloud",
        "family": "rocky-linux-9",
    }


def test_boot_image_params_override_the_default_choice():
    doc = _doc(_instance())
    rule = VmBootImage()
    diff = rule.detect(doc)[0]

    result = rule.apply(
        doc, diff, "stock_image", {"image_project": "debian-cloud", "image_family": "debian-12"}
    )

    assert result.get("compute_instance:web").target_hints["boot_image"]["family"] == "debian-12"


def test_boot_image_rejects_empty_params():
    doc = _doc(_instance())
    rule = VmBootImage()
    diff = rule.detect(doc)[0]
    with pytest.raises(ValueError, match="image_family"):
        rule.validate_params(diff, "stock_image", {"image_project": "p", "image_family": "  "})


def test_boot_image_manual_excludes_the_instance():
    doc = _doc(_instance())
    rule = VmBootImage()
    diff = rule.detect(doc)[0]

    result = rule.apply(doc, diff, "manual")

    assert result.get("compute_instance:web").excluded is not None


# ---- VM-MACHINE-TYPE -------------------------------------------------------


def test_machine_type_exact_match_is_low_risk():
    doc = _doc(_instance(instance_type="m5.xlarge", vcpus=4, memory_mib=16384))
    rule = VmMachineType()
    diff = rule.detect(doc)[0]

    assert diff.context["machine_type"] == "n2-standard-4"
    assert diff.context["memory_delta_mib"] == 0
    assert rule.risk_for(diff, "nearest_predefined") == "low"


def test_machine_type_large_memory_gain_is_medium_risk():
    """c5.xlarge is 2 GiB/vCPU; no GCP predefined type has that ratio."""
    doc = _doc(_instance(instance_type="c5.xlarge", vcpus=4, memory_mib=8192))
    rule = VmMachineType()
    diff = rule.detect(doc)[0]

    assert diff.context["memory_delta_mib"] > 0
    assert rule.risk_for(diff, "nearest_predefined") == "medium"


def test_machine_type_apply_sets_hint_and_delta():
    doc = _doc(_instance())
    rule = VmMachineType()
    diff = rule.detect(doc)[0]

    hints = rule.apply(doc, diff, "nearest_predefined").get("compute_instance:web").target_hints

    assert hints["machine_type"] == "n2-standard-4"
    assert hints["memory_delta_mib"] == 0


def test_custom_machine_type_is_exact():
    doc = _doc(_instance(instance_type="c5.xlarge", vcpus=4, memory_mib=8192))
    rule = VmMachineType()
    diff = rule.detect(doc)[0]

    hints = rule.apply(doc, diff, "custom_machine_type").get("compute_instance:web").target_hints

    assert hints["machine_type"] == "custom-4-8192"


def test_unknown_sizing_offers_only_manual():
    doc = _doc(_instance(instance_type="zz.mega", vcpus=None, memory_mib=None))
    rule = VmMachineType()
    diff = rule.detect(doc)[0]

    assert [s.id for s in rule.allowed_strategies(diff)] == ["manual"]
    assert rule.default_strategy(diff) == "manual"


def test_nearest_predefined_refuses_when_it_has_no_machine_type():
    doc = _doc(_instance(instance_type="zz.mega", vcpus=None, memory_mib=None))
    rule = VmMachineType()
    diff = rule.detect(doc)[0]
    with pytest.raises(ValueError, match="no machine type"):
        rule.apply(doc, diff, "nearest_predefined")


# ---- VM-ZONE-MAPPING -------------------------------------------------------


def test_zones_are_assigned_positionally_by_stable_zone_id():
    doc = _doc(
        _instance(id_="compute_instance:a", zone="ap-south-1b", zone_id="aps1-az2"),
        _instance(id_="compute_instance:b", zone="ap-south-1a", zone_id="aps1-az1"),
    )
    diffs = {d.subjects[0]: d.context["zone_letter"] for d in VmZoneMapping().detect(doc)}

    # aps1-az1 sorts first, so the instance in it gets 'a' regardless of AZ letter.
    assert diffs["compute_instance:b"] == "a"
    assert diffs["compute_instance:a"] == "b"


def test_instances_sharing_an_az_share_a_letter():
    doc = _doc(
        _instance(id_="compute_instance:a", zone="ap-south-1a", zone_id="aps1-az1"),
        _instance(id_="compute_instance:b", zone="ap-south-1a", zone_id="aps1-az1"),
    )
    letters = {d.context["zone_letter"] for d in VmZoneMapping().detect(doc)}
    assert letters == {"a"}


def test_zone_mapping_apply_sets_a_letter_not_a_zone():
    """The GCP region is unknown until plan time, so the rule cannot write a full zone."""
    doc = _doc(_instance())
    rule = VmZoneMapping()
    diff = rule.detect(doc)[0]

    hints = rule.apply(doc, diff, "positional").get("compute_instance:web").target_hints

    assert hints["zone_letter"] == "a"
    assert "ap-south" not in str(hints["zone_letter"])


def test_zone_mapping_does_not_fire_for_templates():
    doc = _doc(_template())
    assert VmZoneMapping().detect(doc) == []


# ---- registration ----------------------------------------------------------


def test_rules_are_registered_in_the_declared_order():
    from easymigrate.differences.rules.aws_gcp import AWS_GCP_RULES

    ids = [rule.id for rule in AWS_GCP_RULES]
    assert "VM-BOOT-IMAGE" in ids
    # Spec 4 §5: a Windows image constrains the machine types available.
    assert ids.index("VM-BOOT-IMAGE") < ids.index("VM-MACHINE-TYPE")
