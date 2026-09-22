"""ELB collection (spec 7 §3). Raw responses only."""

import pytest
from moto import mock_aws

from easymigrate.core.plugin import CollectContext
from easymigrate.plugins.loadbalancing.collect_aws import LOADBALANCING_SNAPSHOT_FILES, collect
from easymigrate.providers.aws.source import connect

AMI = "ami-12c6146b"


@pytest.fixture
def aws(aws_credentials):
    with mock_aws():
        yield connect(region="ap-south-1")


def _stack(aws):
    ec2 = aws.client("ec2")
    vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    subnets = [
        ec2.create_subnet(VpcId=vpc, CidrBlock=c, AvailabilityZone=z)["Subnet"]["SubnetId"]
        for c, z in (("10.0.1.0/24", "ap-south-1a"), ("10.0.2.0/24", "ap-south-1b"))
    ]
    instance = ec2.run_instances(ImageId=AMI, MinCount=1, MaxCount=1, SubnetId=subnets[0])[
        "Instances"
    ][0]["InstanceId"]
    elb = aws.client("elbv2")
    lb = elb.create_load_balancer(Name="web", Subnets=subnets, Type="application")["LoadBalancers"][
        0
    ]
    tg = elb.create_target_group(
        Name="web-tg", Protocol="HTTP", Port=80, VpcId=vpc, HealthCheckPath="/health"
    )["TargetGroups"][0]
    elb.register_targets(TargetGroupArn=tg["TargetGroupArn"], Targets=[{"Id": instance}])
    listener = elb.create_listener(
        LoadBalancerArn=lb["LoadBalancerArn"],
        Protocol="HTTP",
        Port=80,
        DefaultActions=[{"Type": "forward", "TargetGroupArn": tg["TargetGroupArn"]}],
    )["Listeners"][0]
    elb.create_rule(
        ListenerArn=listener["ListenerArn"],
        Priority=10,
        Conditions=[{"Field": "path-pattern", "Values": ["/api/*"]}],
        Actions=[{"Type": "forward", "TargetGroupArn": tg["TargetGroupArn"]}],
    )
    return instance


def test_every_snapshot_file_is_produced(aws):
    assert set(LOADBALANCING_SNAPSHOT_FILES) <= set(collect(aws, CollectContext(plugin="lb")))


def test_load_balancers_listeners_rules_and_targets_are_collected(aws):
    instance = _stack(aws)
    files = collect(aws, CollectContext(plugin="lb"))

    lb = files["loadbalancing.load_balancers"][0]
    assert lb["LoadBalancer"]["LoadBalancerName"] == "web"
    listener = lb["Listeners"][0]
    assert listener["Listener"]["Port"] == 80
    assert any(r.get("Priority") == "10" for r in listener["Rules"])
    group = files["loadbalancing.target_groups"][0]
    assert [t["Target"]["Id"] for t in group["Targets"]] == [instance]
    assert [i["InstanceId"] for i in files["loadbalancing.target_instances"]] == [instance]
