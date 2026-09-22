"""AWS→GCP Kubernetes node-pool rules (spec 5 §5, plan 3)."""

from __future__ import annotations

import pytest

from easymigrate.differences.rules.aws_gcp import AWS_GCP_RULES
from easymigrate.differences.rules.aws_gcp.kubernetes import (
    K8sNodeImage,
    K8sNodeMachineType,
    K8sNodeScaling,
    K8sNodeSpot,
    K8sSelfManagedNodes,
    K8sSshKey,
    node_zone_letters,
)
from easymigrate.ir.base import IRDocument, SourceRef
from easymigrate.ir.kubernetes import KubernetesCluster, NodePool
from tests.plugins.kubernetes.builders import network_objects

CLUSTER = KubernetesCluster(
    id="kubernetes_cluster:prod",
    name="prod",
    source=SourceRef(cloud="aws", type="eks-cluster", id="prod", region="ap-south-1"),
    version="1.31",
)


def _pool(id_="node_pool:workers", **kw):
    fields = dict(
        id=id_,
        name=id_.split(":", 1)[1],
        source=SourceRef(cloud="aws", type="eks-nodegroup", id=id_, region="ap-south-1"),
        cluster=CLUSTER.id,
        origin="managed",
        instance_types=["m5.large"],
        vcpus=2,
        memory_mib=8192,
        min_size=1,
        max_size=4,
        desired_size=3,
        subnets=["subnet:private-1", "subnet:private-2"],
        ami_type="AL2023_x86_64_STANDARD",
    )
    fields.update(kw)
    return NodePool(**fields)


def _doc(*pools):
    return IRDocument(source={"cloud": "aws"}, objects=[*network_objects(), CLUSTER, *pools])


def _only(rule, doc):
    diffs = rule.detect(doc)
    assert len(diffs) == 1
    return diffs[0]


def _hints(doc, pool_id="node_pool:workers"):
    return doc.get(pool_id).target_hints


def test_machine_type_maps_the_primary_type():
    rule = K8sNodeMachineType()
    doc = _doc(_pool())
    diff = _only(rule, doc)
    assert rule.risk_for(diff, "nearest_predefined") == "low"
    assert _hints(rule.apply(doc, diff, "nearest_predefined"))["machine_type"] == "n2-standard-2"


def test_several_instance_types_raise_the_risk():
    rule = K8sNodeMachineType()
    diff = _only(rule, _doc(_pool(instance_types=["m5.large", "m5a.large"])))
    assert rule.risk_for(diff, "nearest_predefined") == "medium"


def test_unknown_sizing_is_manual_only_and_excludes_the_pool():
    rule = K8sNodeMachineType()
    doc = _doc(_pool(vcpus=None, memory_mib=None, instance_types=["x9.huge"]))
    diff = _only(rule, doc)
    assert [s.id for s in rule.allowed_strategies(diff)] == ["manual"]
    assert rule.apply(doc, diff, "manual").get("node_pool:workers").excluded is not None


@pytest.mark.parametrize(
    ("ami", "strategy", "image", "risk"),
    [
        ("AL2023_x86_64_STANDARD", "cos", "COS_CONTAINERD", "low"),
        ("BOTTLEROCKET_x86_64", "ubuntu", "UBUNTU_CONTAINERD", "low"),
        ("CUSTOM", "cos", "COS_CONTAINERD", "medium"),
        ("WINDOWS_CORE_2022_x86_64", "windows", "WINDOWS_LTSC_CONTAINERD", "medium"),
    ],
)
def test_node_image_mapping(ami, strategy, image, risk):
    rule = K8sNodeImage()
    doc = _doc(_pool(ami_type=ami))
    diff = _only(rule, doc)
    assert strategy in {s.id for s in rule.allowed_strategies(diff)}
    assert rule.risk_for(diff, strategy) == risk
    assert _hints(rule.apply(doc, diff, strategy))["image_type"] == image


def test_windows_pool_cannot_take_a_linux_image():
    rule = K8sNodeImage()
    doc = _doc(_pool(ami_type="WINDOWS_FULL_2022_x86_64"))
    diff = _only(rule, doc)
    assert rule.default_strategy(diff) == "windows"
    with pytest.raises(ValueError):
        rule.apply(doc, diff, "cos")


def test_scaling_keeps_totals_and_spreads_desired_per_zone():
    rule = K8sNodeScaling()
    doc = _doc(_pool())
    diff = _only(rule, doc)
    assert diff.context["per_zone"] == 2  # ceil(3 / 2 zones)
    assert diff.context["resulting_total"] == 4
    hints = _hints(rule.apply(doc, diff, "autoscale_totals"))
    assert hints == {
        "zone_letters": ["a", "b"],
        "autoscaling": {"total_min": 1, "total_max": 4, "initial_per_zone": 2},
    }


def test_fixed_size_is_the_only_choice_when_min_equals_max():
    rule = K8sNodeScaling()
    doc = _doc(_pool(min_size=2, max_size=2, desired_size=2))
    diff = _only(rule, doc)
    assert [s.id for s in rule.allowed_strategies(diff)] == ["fixed_size"]
    assert _hints(rule.apply(doc, diff, "fixed_size"))["node_count_per_zone"] == 1


def test_pools_sharing_an_az_share_a_letter():
    doc = _doc(_pool(), _pool("node_pool:gpu", subnets=["subnet:private-2", "subnet:private-3"]))
    assert node_zone_letters(doc) == {"ap-south-1a": "a", "ap-south-1b": "b", "ap-south-1c": "c"}


def test_spot_pool_sets_the_hint():
    rule = K8sNodeSpot()
    doc = _doc(_pool(capacity="spot"))
    assert _hints(rule.apply(doc, _only(rule, doc), "spot"))["spot"] is True
    assert rule.detect(_doc(_pool())) == []


def test_self_managed_pool_is_high_risk_or_omitted():
    rule = K8sSelfManagedNodes()
    doc = _doc(_pool(origin="self_managed", ami_type="CUSTOM"))
    diff = _only(rule, doc)
    assert rule.risk_for(diff, "node_pool") == "high"
    assert rule.apply(doc, diff, "omit").get("node_pool:workers").excluded is not None
    assert rule.detect(_doc(_pool())) == []


def test_ssh_key_is_noted():
    assert len(K8sSshKey().detect(_doc(_pool(ssh_key="ops")))) == 1
    assert K8sSshKey().detect(_doc(_pool())) == []


def test_node_rules_run_after_cluster_rules():
    ids = [rule.id for rule in AWS_GCP_RULES]
    assert ids.index("K8S-FARGATE") < ids.index("K8S-SELF-MANAGED-NODES")
    assert ids.index("K8S-NODE-MACHINE-TYPE") < ids.index("K8S-NODE-IMAGE")
