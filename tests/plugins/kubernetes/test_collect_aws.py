"""EKS collection (spec 5 §4.1). Raw responses only."""

import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from easymigrate.core.plugin import CollectContext
from easymigrate.plugins.kubernetes.collect_aws import KUBERNETES_SNAPSHOT_FILES, collect
from easymigrate.providers.aws.source import connect

ROLE = "arn:aws:iam::123456789012:role/eks"


@pytest.fixture
def aws():
    with mock_aws():
        yield connect(region="ap-south-1")


def _network(ec2):
    vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    subnets = [
        ec2.create_subnet(VpcId=vpc, CidrBlock=cidr, AvailabilityZone=zone)["Subnet"]["SubnetId"]
        for cidr, zone in (("10.0.1.0/24", "ap-south-1a"), ("10.0.2.0/24", "ap-south-1b"))
    ]
    return vpc, subnets


def _cluster(aws, name="prod"):
    _, subnets = _network(aws.client("ec2"))
    eks = aws.client("eks")
    eks.create_cluster(name=name, roleArn=ROLE, resourcesVpcConfig={"subnetIds": subnets})
    return eks, subnets


def test_every_snapshot_file_is_produced(aws):
    files = collect(aws, CollectContext(plugin="kubernetes"))
    assert set(KUBERNETES_SNAPSHOT_FILES) <= set(files)


def test_clusters_node_groups_and_fargate_are_described(aws):
    eks, subnets = _cluster(aws)
    eks.create_nodegroup(
        clusterName="prod",
        nodegroupName="workers",
        nodeRole=ROLE,
        subnets=subnets,
        instanceTypes=["m5.large"],
        scalingConfig={"minSize": 1, "maxSize": 3, "desiredSize": 2},
    )
    eks.create_fargate_profile(
        clusterName="prod",
        fargateProfileName="batch",
        podExecutionRoleArn=ROLE,
        subnets=subnets,
        selectors=[{"namespace": "batch"}],
    )

    files = collect(aws, CollectContext(plugin="kubernetes"))

    assert files["kubernetes.clusters"]["prod"]["name"] == "prod"
    assert [ng["nodegroupName"] for ng in files["kubernetes.nodegroups"]["prod"]] == ["workers"]
    profiles = files["kubernetes.fargate_profiles"]["prod"]
    assert [p["fargateProfileName"] for p in profiles] == ["batch"]
    types = [t["InstanceType"] for t in files["kubernetes.instance_types"]["InstanceTypes"]]
    assert types == ["m5.large"]


def test_self_managed_groups_are_those_tagged_for_a_cluster(aws):
    _, subnets = _network(aws.client("ec2"))
    template = aws.client("ec2").create_launch_template(
        LaunchTemplateName="nodes", LaunchTemplateData={"InstanceType": "c5.large"}
    )["LaunchTemplate"]
    autoscaling = aws.client("autoscaling")
    for name, tags in (
        ("self-nodes", [{"Key": "kubernetes.io/cluster/prod", "Value": "owned"}]),
        ("plain-web", []),
    ):
        autoscaling.create_auto_scaling_group(
            AutoScalingGroupName=name,
            MinSize=1,
            MaxSize=2,
            VPCZoneIdentifier=subnets[0],
            LaunchTemplate={"LaunchTemplateId": template["LaunchTemplateId"], "Version": "1"},
            Tags=[{**t, "PropagateAtLaunch": True} for t in tags],
        )

    files = collect(aws, CollectContext(plugin="kubernetes"))

    groups = files["kubernetes.self_managed_groups"]["AutoScalingGroups"]
    assert [g["AutoScalingGroupName"] for g in groups] == ["self-nodes"]
    assert (
        files["kubernetes.launch_templates"][template["LaunchTemplateId"]]["1"]["InstanceType"]
        == "c5.large"
    )


def test_denied_describe_cluster_is_a_scan_gap_and_the_rest_continues(aws, monkeypatch):
    _cluster(aws)
    original = type(aws).client

    def client(self, service):
        c = original(self, service)

        def deny(**kwargs):
            raise ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "no"}}, "DescribeCluster"
            )

        c.meta.events.register("before-call.eks.DescribeCluster", deny)
        return c

    monkeypatch.setattr(type(aws), "client", client)
    ctx = CollectContext(plugin="kubernetes")
    files = collect(aws, ctx)

    assert files["kubernetes.clusters"] == {}
    assert "Grant eks:DescribeCluster" in [f.suggestion for f in ctx.findings]


def test_access_entries_not_applicable_to_the_cluster_do_not_fail_the_scan(aws, monkeypatch):
    """Live run, 2026-09-22: EKS rejects ListAccessEntries on a CONFIG_MAP-mode cluster."""
    _cluster(aws)
    original = type(aws).client

    def client(self, service):
        c = original(self, service)

        def reject(**kwargs):
            raise ClientError(
                {"Error": {"Code": "InvalidRequestException", "Message": "mode"}},
                "ListAccessEntries",
            )

        c.meta.events.register("before-call.eks.ListAccessEntries", reject)
        return c

    monkeypatch.setattr(type(aws), "client", client)
    files = collect(aws, CollectContext(plugin="kubernetes"))
    assert files["kubernetes.access_entries"]["prod"] == []
    assert "prod" in files["kubernetes.clusters"]
