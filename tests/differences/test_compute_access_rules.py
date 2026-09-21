"""AWS→GCP compute access and identity rules (spec 4 §5, plan 4)."""

from __future__ import annotations

import pytest

from easymigrate.differences.rules.aws_gcp import AWS_GCP_RULES
from easymigrate.differences.rules.aws_gcp.compute import (
    VmElasticIpUnattached,
    VmIamProfile,
    VmImds,
    VmPlacementGroup,
    VmPublicIp,
    VmSpot,
    VmSshKeys,
    VmTenancy,
    VmTerminationProtection,
    VmUserData,
)
from easymigrate.ir.base import IRDocument, SourceRef
from easymigrate.ir.compute import ComputeInstance, ExternalAddress, ImdsOptions, InstanceTemplate
from easymigrate.ir.network import Network, Subnet

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIB7 web@host"


def _source(type_: str, native: str) -> SourceRef:
    return SourceRef(cloud="aws", type=type_, id=native, region="ap-south-1")


def _instance(id_="compute_instance:web", **kw):
    fields = dict(
        id=id_,
        name=id_.split(":", 1)[1],
        source=_source("instance", id_),
        instance_type="m5.large",
        vcpus=2,
        memory_mib=8192,
        architecture="x86_64",
        state="running",
        zone="ap-south-1a",
        subnet="subnet:a",
        image="ami-1",
        platform="linux",
        platform_details="Linux/UNIX",
    )
    fields.update(kw)
    return ComputeInstance(**fields)


def _template(**kw):
    fields = dict(
        id="instance_template:lt",
        name="lt",
        source=_source("launch-template", "lt-1:1"),
        instance_type="t3.small",
        architecture="x86_64",
        image="ami-1",
        platform="linux",
        version="1",
    )
    fields.update(kw)
    return InstanceTemplate(**fields)


def _address(id_="external_address:spare", attached=False):
    return ExternalAddress(
        id=id_,
        name=id_.split(":", 1)[1],
        source=_source("elastic-ip", "eipalloc-1"),
        address="13.0.0.1",
        attached=attached,
    )


def _doc(*objects) -> IRDocument:
    network = Network(
        id="network:main",
        name="main",
        source=_source("vpc", "vpc-1"),
        cidr_blocks=["10.0.0.0/16"],
        dns_hostnames=True,
        dns_resolution=True,
    )
    subnet = Subnet(
        id="subnet:a",
        name="a",
        source=_source("subnet", "subnet-1"),
        network="network:main",
        cidr="10.0.1.0/24",
        zone="ap-south-1a",
        internet="public",
        map_public_ip=False,
    )
    return IRDocument(source={"cloud": "aws"}, objects=[network, subnet, *objects])


def _only(rule, doc):
    diffs = rule.detect(doc)
    assert len(diffs) == 1
    return diffs[0]


def _hints(doc, obj_id="compute_instance:web"):
    return doc.get(obj_id).target_hints


# ---- VM-SSH-KEYS -----------------------------------------------------------


def test_ssh_keys_fires_only_with_a_key_pair():
    assert VmSshKeys().detect(_doc(_instance())) == []
    assert len(VmSshKeys().detect(_doc(_instance(key_pair="deploy")))) == 1


def test_ssh_keys_imports_the_public_key_for_the_amis_login():
    rule = VmSshKeys()
    doc = _doc(_instance(key_pair="deploy", key_material=KEY, platform_details="Ubuntu"))
    diff = _only(rule, doc)
    assert rule.default_strategy(diff) == "import_public_key"
    params = rule.default_params(diff, "import_public_key")
    assert params == {"username": "ubuntu"}

    result = rule.apply(doc, diff, "import_public_key", params)
    assert _hints(result)["metadata"] == {"ssh-keys": f"ubuntu:{KEY}"}


def test_ssh_keys_without_key_material_defaults_to_os_login():
    rule = VmSshKeys()
    diff = _only(rule, _doc(_instance(key_pair="deploy")))
    assert "import_public_key" not in {s.id for s in rule.allowed_strategies(diff)}
    assert rule.default_strategy(diff) == "os_login"
    assert rule.risk_for(diff, "os_login") == "medium"


def test_ssh_keys_os_login_and_no_access_write_metadata():
    rule = VmSshKeys()
    doc = _doc(_instance(key_pair="deploy"))
    diff = _only(rule, doc)
    assert _hints(rule.apply(doc, diff, "os_login"))["metadata"] == {"enable-oslogin": "TRUE"}
    assert _hints(rule.apply(doc, diff, "no_ssh_access"))["metadata"] == {
        "block-project-ssh-keys": "TRUE"
    }


def test_ssh_keys_user_supplied_key_is_validated():
    rule = VmSshKeys()
    doc = _doc(_instance(key_pair="deploy"))
    diff = _only(rule, doc)
    defaults = rule.default_params(diff, "user_supplied_key")
    with pytest.raises(ValueError, match="public_key"):
        rule.validate_params(diff, "user_supplied_key", defaults)
    with pytest.raises(ValueError, match="username"):
        rule.validate_params(diff, "user_supplied_key", {"username": "Root!", "public_key": KEY})

    result = rule.apply(
        doc, diff, "user_supplied_key", {"username": "ops", "public_key": f"  {KEY}\n"}
    )
    assert _hints(result)["metadata"] == {"ssh-keys": f"ops:{KEY}"}


def test_ssh_keys_windows_offers_only_no_ssh_access():
    rule = VmSshKeys()
    diff = _only(rule, _doc(_instance(key_pair="deploy", key_material=KEY, platform="windows")))
    allowed = rule.allowed_strategies(diff)
    assert [s.id for s in allowed] == ["no_ssh_access"]
    assert "reset-windows-password" in allowed[0].effect
    assert rule.default_strategy(diff) == "no_ssh_access"


def test_ssh_keys_applies_to_templates():
    assert len(VmSshKeys().detect(_doc(_template(key_pair="deploy")))) == 1


# ---- VM-USER-DATA ----------------------------------------------------------


def test_user_data_becomes_a_startup_script():
    rule = VmUserData()
    doc = _doc(_instance(user_data="#!/bin/bash\necho hi\n"))
    diff = _only(rule, doc)
    assert rule.default_strategy(diff) == "startup_script"
    assert rule.strategy("startup_script").risk == "medium"
    assert "every boot" in rule.strategy("startup_script").effect
    result = rule.apply(doc, diff, "startup_script")
    assert _hints(result)["metadata"] == {"startup-script": "#!/bin/bash\necho hi\n"}


def test_cloud_config_defaults_to_cloud_init():
    rule = VmUserData()
    doc = _doc(_instance(user_data="#cloud-config\npackages: [nginx]\n"))
    diff = _only(rule, doc)
    assert rule.default_strategy(diff) == "cloud_init"
    result = rule.apply(doc, diff, "cloud_init")
    assert _hints(result)["metadata"] == {"user-data": "#cloud-config\npackages: [nginx]\n"}


def test_windows_powershell_is_unwrapped_and_keyed_by_language():
    rule = VmUserData()
    doc = _doc(
        _instance(platform="windows", user_data="<powershell>\nWrite-Host hi\n</powershell>")
    )
    diff = _only(rule, doc)
    assert "cloud_init" not in {s.id for s in rule.allowed_strategies(diff)}
    result = rule.apply(doc, diff, "startup_script")
    assert _hints(result)["metadata"] == {"windows-startup-script-ps1": "Write-Host hi"}
    with pytest.raises(ValueError):
        rule.apply(doc, diff, "cloud_init")


def test_user_data_omit_leaves_no_metadata_and_blank_is_ignored():
    rule = VmUserData()
    doc = _doc(_instance(user_data="echo hi"))
    assert "metadata" not in _hints(rule.apply(doc, _only(rule, doc), "omit"))
    assert rule.detect(_doc(_instance(user_data="  \n"))) == []


def test_metadata_from_two_rules_merges():
    doc = _doc(_instance(key_pair="deploy", user_data="echo hi"))
    doc = VmSshKeys().apply(doc, _only(VmSshKeys(), doc), "os_login")
    doc = VmUserData().apply(doc, _only(VmUserData(), doc), "startup_script")
    assert _hints(doc)["metadata"] == {"enable-oslogin": "TRUE", "startup-script": "echo hi"}


# ---- manual, acknowledge-only rules -----------------------------------------


@pytest.mark.parametrize(
    ("rule", "instance"),
    [
        (VmIamProfile(), _instance(iam_profile="arn:aws:iam::1:instance-profile/app")),
        (VmTenancy(), _instance(tenancy="dedicated")),
        (VmPlacementGroup(), _instance(placement_group="tight")),
    ],
)
def test_manual_rules_need_acknowledgement_but_keep_the_instance(rule, instance):
    doc = _doc(instance)
    diff = _only(rule, doc)
    assert rule.strategy(rule.default).manual is True
    result = rule.apply(doc, diff, "manual")
    assert result.get(instance.id).excluded is None


def test_manual_rules_do_not_fire_on_a_plain_instance():
    doc = _doc(_instance())
    for rule in (VmIamProfile(), VmTenancy(), VmPlacementGroup(), VmImds(), VmSpot()):
        assert rule.detect(doc) == []


def test_imds_fires_only_when_tokens_are_required():
    optional = _instance(imds=ImdsOptions(http_tokens="optional", hop_limit=1))
    required = _instance(imds=ImdsOptions(http_tokens="required", hop_limit=2))
    assert VmImds().detect(_doc(optional)) == []
    diff = _only(VmImds(), _doc(required))
    assert VmImds().risk_for(diff, "note_only") == "none"


# ---- VM-SPOT / VM-TERMINATION-PROTECTION -------------------------------------


def test_spot_vm_sets_the_hint_and_standard_does_not():
    rule = VmSpot()
    doc = _doc(_instance(spot=True))
    diff = _only(rule, doc)
    assert _hints(rule.apply(doc, diff, "spot_vm"))["spot"] is True
    assert "spot" not in _hints(rule.apply(doc, diff, "standard"))


def test_termination_protection_becomes_deletion_protection():
    rule = VmTerminationProtection()
    doc = _doc(_instance(termination_protection=True))
    diff = _only(rule, doc)
    assert rule.risk_for(diff, "deletion_protection") == "none"
    assert _hints(rule.apply(doc, diff, "deletion_protection"))["deletion_protection"] is True


# ---- VM-ELASTIC-IP-UNATTACHED / VM-PUBLIC-IP ----------------------------------


def test_unattached_eip_is_reserved_or_excluded():
    rule = VmElasticIpUnattached()
    doc = _doc(_address(), _address("external_address:used", attached=True))
    diff = _only(rule, doc)
    assert diff.subjects == ["external_address:spare"]
    assert _hints(rule.apply(doc, diff, "reserve_static"), "external_address:spare") == {
        "reserve": True
    }
    assert rule.apply(doc, diff, "omit").get("external_address:spare").excluded is not None


def test_static_external_is_offered_only_for_an_elastic_ip():
    rule = VmPublicIp()
    ephemeral = _only(rule, _doc(_instance(external_ip="ephemeral")))
    assert "static_external" not in {s.id for s in rule.allowed_strategies(ephemeral)}
    doc = _doc(_address(attached=True), _instance(external_ip="external_address:spare"))
    elastic = _only(rule, doc)
    assert "static_external" in {s.id for s in rule.allowed_strategies(elastic)}
    assert _hints(rule.apply(doc, elastic, "static_external"))["external"] == "static"


def test_elastic_ip_rule_runs_after_public_ip():
    ids = [rule.id for rule in AWS_GCP_RULES]
    assert ids.index("VM-PUBLIC-IP") < ids.index("VM-ELASTIC-IP-UNATTACHED")
