"""Kubernetes normalization (spec 5 §4.2, §4.3). Faithful capture only."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from easymigrate.core.plugin import NormalizeContext, Snapshot
from easymigrate.differences.membership import policy_members
from easymigrate.ir.base import IRDocument
from easymigrate.ir.kubernetes import KubernetesCluster, NodePool
from easymigrate.plugins.kubernetes.normalize import normalize
from tests.plugins.kubernetes import builders as b


def run(files: dict[str, Any]):
    ctx = NormalizeContext(account_id="1", region="ap-south-1", strict=False)
    ctx.objects = {obj.id: obj for obj in b.network_objects()}
    return normalize(Snapshot(files), ctx), ctx


def only(objects, cls):
    return [obj for obj in objects if isinstance(obj, cls)]


def codes(ctx):
    return [f.code for f in ctx.findings]


def test_cluster_is_captured_faithfully():
    objects, _ = run(b.snapshot_files())
    cluster = only(objects, KubernetesCluster)[0]
    assert cluster.id == "kubernetes_cluster:prod"
    assert cluster.version == "1.31"
    assert cluster.subnets == ["subnet:private-1", "subnet:private-2"]
    assert cluster.firewall_policies == ["firewall_policy:cluster-sg"]
    assert cluster.public_access_cidrs == ["0.0.0.0/0"]
    assert cluster.service_cidr == "172.20.0.0/16"
    assert cluster.log_types == ["api", "audit"]
    assert cluster.oidc_issuer.endswith("/id/ABC")
    assert cluster.labels == {"team": "platform"}


def test_managed_node_group_becomes_a_node_pool():
    objects, _ = run(b.snapshot_files())
    pool = only(objects, NodePool)[0]
    assert pool.cluster == "kubernetes_cluster:prod"
    assert pool.origin == "managed"
    assert (pool.vcpus, pool.memory_mib) == (2, 8192)  # from the fallback table
    assert (pool.min_size, pool.desired_size, pool.max_size) == (1, 2, 4)
    assert pool.node_labels == {"role": "general"}
    assert pool.taints[0].effect == "NO_SCHEDULE"
    # Without a launch template naming groups, EKS attaches the cluster security group.
    assert pool.firewall_policies == ["firewall_policy:cluster-sg"]
    assert pool.disk_size_gib == 50


def test_launch_template_security_groups_replace_the_cluster_group():
    nodegroup = b.nodegroup(launchTemplate={"id": "lt-1", "version": "2"}, instanceTypes=[])
    files = b.snapshot_files(
        **{
            "kubernetes.nodegroups": {"prod": [nodegroup]},
            "kubernetes.launch_templates": {
                "lt-1": {"2": {"InstanceType": "c5.xlarge", "SecurityGroupIds": ["sg-nodes"]}}
            },
        }
    )
    pool = only(run(files)[0], NodePool)[0]
    assert pool.firewall_policies == ["firewall_policy:nodes-sg"]
    assert pool.instance_types == ["c5.xlarge"]


def test_self_managed_group_becomes_a_node_pool_from_its_launch_template():
    group = {
        "AutoScalingGroupName": "self-nodes",
        "MinSize": 1,
        "MaxSize": 3,
        "DesiredCapacity": 1,
        "VPCZoneIdentifier": "subnet-1,subnet-3",
        "LaunchTemplate": {"LaunchTemplateId": "lt-self", "Version": "1"},
        "Tags": [{"Key": "kubernetes.io/cluster/prod", "Value": "owned"}],
    }
    files = b.snapshot_files(
        **{
            "kubernetes.self_managed_groups": {"AutoScalingGroups": [group]},
            "kubernetes.launch_templates": {
                "lt-self": {
                    "1": {
                        "InstanceType": "r5.large",
                        "SecurityGroupIds": ["sg-nodes"],
                        "KeyName": "ops",
                        "BlockDeviceMappings": [
                            {"DeviceName": "/dev/xvda", "Ebs": {"VolumeSize": 80}}
                        ],
                    }
                }
            },
        }
    )
    pool = next(p for p in only(run(files)[0], NodePool) if p.origin == "self_managed")
    assert pool.cluster == "kubernetes_cluster:prod"
    assert pool.ami_type == "CUSTOM"
    assert pool.instance_types == ["r5.large"]
    assert pool.subnets == ["subnet:private-1", "subnet:private-3"]
    assert (pool.disk_size_gib, pool.ssh_key) == (80, "ops")


def test_self_managed_group_for_an_unscanned_cluster_is_reported():
    group = {
        "AutoScalingGroupName": "other",
        "MinSize": 0,
        "MaxSize": 1,
        "DesiredCapacity": 0,
        "Tags": [{"Key": "kubernetes.io/cluster/elsewhere", "Value": "owned"}],
    }
    files = b.snapshot_files(**{"kubernetes.self_managed_groups": {"AutoScalingGroups": [group]}})
    objects, ctx = run(files)
    assert all(p.origin == "managed" for p in only(objects, NodePool))
    assert "K8S-SELF-MANAGED-ORPHAN" in codes(ctx)


def test_inactive_cluster_and_node_group_are_reported_not_translated():
    files = b.snapshot_files(
        **{"kubernetes.nodegroups": {"prod": [b.nodegroup(status="CREATE_FAILED")]}}
    )
    objects, ctx = run(files)
    assert only(objects, NodePool) == []
    assert "K8S-NODEGROUP-NOT-ACTIVE" in codes(ctx)

    files = b.snapshot_files(**{"kubernetes.clusters": {"prod": b.cluster(status="CREATING")}})
    objects, ctx = run(files)
    assert objects == []
    assert "K8S-CLUSTER-NOT-ACTIVE" in codes(ctx)


def test_fargate_addons_access_and_pod_identities_are_captured_on_the_cluster():
    files = b.snapshot_files(
        **{
            "kubernetes.fargate_profiles": {
                "prod": [{"fargateProfileName": "batch", "selectors": [{"namespace": "batch"}]}]
            },
            "kubernetes.addons": {
                "prod": [{"addonName": "vpc-cni", "addonVersion": "v1.18.0-eksbuild.1"}]
            },
            "kubernetes.access_entries": {"prod": ["arn:aws:iam::1:role/admin"]},
            "kubernetes.pod_identities": {
                "prod": [{"namespace": "app", "serviceAccount": "api", "roleArn": "arn:role/api"}]
            },
        }
    )
    cluster = only(run(files)[0], KubernetesCluster)[0]
    assert cluster.fargate_profiles[0].selectors[0].namespace == "batch"
    assert cluster.addons[0].name == "vpc-cni"
    assert cluster.access_entries == ["arn:aws:iam::1:role/admin"]
    assert cluster.pod_identities[0].service_account == "api"


def test_unresolved_subnet_is_omitted_with_a_note():
    files = b.snapshot_files(
        **{"kubernetes.nodegroups": {"prod": [b.nodegroup(subnets=["subnet-9"])]}}
    )
    objects, ctx = run(files)
    assert only(objects, NodePool)[0].subnets == []
    assert "K8S-REF-UNRESOLVED" in codes(ctx)


def test_node_pools_count_as_policy_members():
    """DB-SG-FIREWALL must see a security group shared by RDS and a cluster (spec 5 §4.2)."""
    objects, _ = run(b.snapshot_files())
    doc = IRDocument(source={"cloud": "aws"}, objects=[*b.network_objects(), *objects])
    assert "node_pool:workers" in policy_members(doc)["firewall_policy:cluster-sg"]


def test_node_pool_rejects_unordered_sizes_and_bad_taints():
    pool = only(run(b.snapshot_files())[0], NodePool)[0]
    with pytest.raises(ValidationError):
        NodePool.model_validate({**pool.model_dump(), "desired_size": 9})
    with pytest.raises(ValidationError):
        NodePool.model_validate(
            {**pool.model_dump(), "taints": [{"key": "k", "effect": "NoSchedule"}]}
        )


def test_node_pool_referencing_a_missing_cluster_fails_integrity():
    objects, _ = run(b.snapshot_files())
    pools = only(objects, NodePool)
    with pytest.raises(ValidationError):
        IRDocument(source={"cloud": "aws"}, objects=[*b.network_objects(), *pools])
