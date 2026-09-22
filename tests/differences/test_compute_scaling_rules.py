"""AWS→GCP compute scaling rules (spec 4 §5, plan 5)."""

from __future__ import annotations

import pytest

from easymigrate.differences.rules.aws_gcp import AWS_GCP_RULES
from easymigrate.differences.rules.aws_gcp.compute import (
    VmAsgHealthcheck,
    VmAsgLifecycle,
    VmAsgMixedInstances,
    VmAsgScaling,
    VmAsgShape,
    VmAsgTargetGroups,
    VmZoneMapping,
)
from easymigrate.ir.base import IRDocument, SourceRef
from easymigrate.ir.compute import ComputeInstance, InstanceGroup, InstanceTemplate, ScalingPolicy
from easymigrate.ir.network import Network, Subnet


def _source(type_: str, native: str) -> SourceRef:
    return SourceRef(cloud="aws", type=type_, id=native, region="ap-south-1")


def _subnet(id_, zone):
    return Subnet(
        id=id_,
        name=id_.split(":", 1)[1],
        source=_source("subnet", id_),
        network="network:main",
        cidr="10.0.1.0/24" if zone.endswith("a") else "10.0.2.0/24",
        zone=zone,
        internet="isolated",
        map_public_ip=False,
    )


def _template(**kw):
    return InstanceTemplate(
        id="instance_template:lt",
        name="lt",
        source=_source("launch-template", "lt-1:1"),
        instance_type="t3.small",
        architecture="x86_64",
        image="ami-1",
        platform="linux",
        version="1",
        **kw,
    )


def _group(**kw):
    fields = dict(
        id="instance_group:web",
        name="web",
        source=_source("autoScalingGroup", "web"),
        template="instance_template:lt",
        min_size=1,
        max_size=4,
        desired_capacity=2,
        zones=["ap-south-1a", "ap-south-1b"],
        subnets=["subnet:b", "subnet:a"],
    )
    fields.update(kw)
    return InstanceGroup(**fields)


def _instance(zone="ap-south-1b", zone_id="aps1-az3"):
    return ComputeInstance(
        id="compute_instance:vm",
        name="vm",
        source=_source("instance", "i-1"),
        instance_type="t3.small",
        architecture="x86_64",
        state="running",
        zone=zone,
        zone_id=zone_id,
        subnet="subnet:b",
        image="ami-1",
        platform="linux",
    )


def _doc(*objects, template=None) -> IRDocument:
    network = Network(
        id="network:main",
        name="main",
        source=_source("vpc", "vpc-1"),
        cidr_blocks=["10.0.0.0/16"],
        dns_hostnames=True,
        dns_resolution=True,
    )
    return IRDocument(
        source={"cloud": "aws"},
        objects=[
            network,
            _subnet("subnet:a", "ap-south-1a"),
            _subnet("subnet:b", "ap-south-1b"),
            template or _template(),
            *objects,
        ],
    )


def _only(rule, doc):
    diffs = rule.detect(doc)
    assert len(diffs) == 1
    return diffs[0]


def _hints(doc):
    return doc.get("instance_group:web").target_hints


# ---- zone mapping -----------------------------------------------------------


def test_group_zones_share_letters_with_instances_in_the_same_az():
    doc = _doc(_group(), _instance())
    letters = VmZoneMapping._letters(doc)
    assert letters["ap-south-1b"] == "a"  # the instance's zone id sorts before unnamed zones
    assert letters["ap-south-1a"] == "b"
    instance_diff = _only(VmZoneMapping(), doc)
    shape_diff = _only(VmAsgShape(), doc)
    assert instance_diff.context["zone_letter"] in shape_diff.context["zone_letters"]


# ---- VM-ASG-SHAPE -------------------------------------------------------------


def test_two_az_group_becomes_a_regional_mig():
    rule = VmAsgShape()
    doc = _doc(_group())
    diff = _only(rule, doc)
    assert rule.default_strategy(diff) == "regional_mig"
    hints = _hints(rule.apply(doc, diff, "regional_mig"))
    assert hints == {"mig": "regional", "zone_letters": ["a", "b"], "subnet": "subnet:a"}


def test_one_az_group_defaults_to_zonal_at_low_risk():
    rule = VmAsgShape()
    doc = _doc(_group(zones=["ap-south-1a"], subnets=["subnet:a"]))
    diff = _only(rule, doc)
    assert rule.default_strategy(diff) == "zonal_mig"
    assert rule.risk_for(diff, "zonal_mig") == "low"
    assert _hints(rule.apply(doc, diff, "zonal_mig"))["zone_letters"] == ["a"]


def test_zonal_choice_for_a_two_az_group_is_medium_and_keeps_one_zone():
    rule = VmAsgShape()
    doc = _doc(_group())
    diff = _only(rule, doc)
    assert rule.risk_for(diff, "zonal_mig") == "medium"
    assert _hints(rule.apply(doc, diff, "zonal_mig"))["zone_letters"] == ["a"]


def test_group_whose_template_was_excluded_is_excluded_too():
    rule = VmAsgShape()
    excluded = _template().model_copy(update={"excluded": "VM-MACHINE-TYPE: handled manually"})
    doc = _doc(_group(), template=excluded)
    result = rule.apply(doc, _only(rule, doc), "regional_mig")
    assert "template is not migrated" in result.get("instance_group:web").excluded


# ---- VM-ASG-HEALTHCHECK -------------------------------------------------------


def test_elb_group_defaults_to_http_autohealing_with_the_grace_period():
    rule = VmAsgHealthcheck()
    doc = _doc(_group(health_check_type="ELB", health_check_grace=120))
    diff = _only(rule, doc)
    assert rule.default_strategy(diff) == "autohealing_http"
    params = {"port": 8080, "request_path": "/healthz"}
    hints = _hints(rule.apply(doc, diff, "autohealing_http", params))
    assert hints == {"health_check": params, "initial_delay_sec": 120}


def test_ec2_group_defaults_to_no_autohealing():
    rule = VmAsgHealthcheck()
    doc = _doc(_group())
    diff = _only(rule, doc)
    assert rule.default_strategy(diff) == "no_autohealing"
    assert _hints(rule.apply(doc, diff, "no_autohealing")) == {}


@pytest.mark.parametrize(
    "params",
    [{"port": 0, "request_path": "/"}, {"port": True, "request_path": "/"}, {"port": "80"}],
)
def test_healthcheck_params_are_validated(params):
    rule = VmAsgHealthcheck()
    diff = _only(rule, _doc(_group(health_check_type="ELB")))
    with pytest.raises(ValueError):
        rule.validate_params(diff, "autohealing_http", {"request_path": "/", **params})
    with pytest.raises(ValueError, match="request_path"):
        rule.validate_params(diff, "autohealing_http", {"port": 80, "request_path": "health"})


# ---- VM-ASG-SCALING -----------------------------------------------------------

CPU = ScalingPolicy(
    name="cpu",
    policy_type="TargetTrackingScaling",
    metric="ASGAverageCPUUtilization",
    target_value=60.0,
)


def test_cpu_target_tracking_becomes_an_autoscaler():
    rule = VmAsgScaling()
    doc = _doc(_group(scaling_policies=[CPU]))
    diff = _only(rule, doc)
    assert rule.default_strategy(diff) == "cpu_target"
    assert _hints(rule.apply(doc, diff, "cpu_target")) == {"autoscaler": {"cpu_target": 0.6}}
    assert _hints(rule.apply(doc, diff, "fixed_size")) == {}


def test_step_scaling_is_manual_only():
    rule = VmAsgScaling()
    step = ScalingPolicy(name="step", policy_type="StepScaling")
    doc = _doc(_group(scaling_policies=[step]))
    diff = _only(rule, doc)
    assert [s.id for s in rule.allowed_strategies(diff)] == ["manual"]
    assert rule.default_strategy(diff) == "manual"
    with pytest.raises(ValueError):
        rule.apply(doc, diff, "cpu_target")


def test_no_policies_means_no_scaling_difference():
    assert VmAsgScaling().detect(_doc(_group())) == []


# ---- note and acknowledge rules -------------------------------------------------


def test_target_groups_are_dropped_at_high_risk():
    rule = VmAsgTargetGroups()
    diff = _only(rule, _doc(_group(target_groups=["arn:tg"])))
    assert rule.risk_for(diff, "drop_with_note") == "high"


def test_mixed_instances_needs_acknowledgement_and_keeps_the_group():
    rule = VmAsgMixedInstances()
    doc = _doc(_group(mixed_instances=True))
    assert rule.strategy("manual").manual is True
    result = rule.apply(doc, _only(rule, doc), "manual")
    assert result.get("instance_group:web").excluded is None


def test_lifecycle_hooks_are_noted():
    rule = VmAsgLifecycle()
    diff = _only(rule, _doc(_group(lifecycle_hooks=["drain"])))
    assert diff.context["hooks"] == ["drain"]
    assert rule.detect(_doc(_group())) == []


def test_asg_shape_runs_first_among_the_scaling_rules():
    ids = [rule.id for rule in AWS_GCP_RULES]
    asg = [i for i in ids if i.startswith("VM-ASG-")]
    assert asg[0] == "VM-ASG-SHAPE"
    assert ids.index("VM-MACHINE-TYPE") < ids.index("VM-ASG-SHAPE")


def test_group_zone_ids_sort_with_instance_zone_ids():
    """Live run, 2026-09-22: without its ids an ASG's AZs sorted last and landed in zone c."""
    group = _group(zone_ids={"ap-south-1a": "aps1-az1", "ap-south-1b": "aps1-az3"})
    other = _instance(zone="ap-south-1c", zone_id="aps1-az2")
    letters = VmZoneMapping._letters(_doc(group, other))
    assert letters == {"ap-south-1a": "a", "ap-south-1c": "b", "ap-south-1b": "c"}


def test_an_excluded_instance_takes_no_zone():
    """Live run, 2026-09-22: `trineural`, excluded by a manual decision, still took a letter."""
    excluded = _instance(zone="ap-south-1c", zone_id="aps1-az2").model_copy(
        update={"excluded": "VM-BOOT-IMAGE: handled manually"}
    )
    group = _group(zone_ids={"ap-south-1a": "aps1-az1", "ap-south-1b": "aps1-az3"})
    doc = _doc(group, excluded)
    shape = VmAsgShape()
    result = shape.apply(doc, _only(shape, doc), "regional_mig")
    assert _hints(result)["zone_letters"] == ["a", "b"]
