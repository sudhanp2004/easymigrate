"""Compute collection (spec 4 §4.1). Raw AWS responses only; no interpretation."""

import base64

import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from easymigrate.core.plugin import CollectContext
from easymigrate.plugins.compute.collect_aws import (
    COMPUTE_SNAPSHOT_FILES,
    _collect_images,
    collect,
)
from easymigrate.providers.aws import source
from easymigrate.providers.aws.source import connect

AMI = "ami-12c6146b"  # moto's canned Amazon Linux image id
USER_DATA = "#!/bin/bash\necho hello\n"


def _network(ec2):
    vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    subnet = ec2.create_subnet(VpcId=vpc, CidrBlock="10.0.1.0/24", AvailabilityZone="ap-south-1a")[
        "Subnet"
    ]["SubnetId"]
    sg = ec2.create_security_group(GroupName="web-sg", Description="web", VpcId=vpc)["GroupId"]
    return vpc, subnet, sg


def _instance(ec2, subnet, sg, *, name="web", user_data=USER_DATA):
    return ec2.run_instances(
        ImageId=AMI,
        MinCount=1,
        MaxCount=1,
        InstanceType="t3.micro",
        SubnetId=subnet,
        SecurityGroupIds=[sg],
        UserData=user_data,
        TagSpecifications=[{"ResourceType": "instance", "Tags": [{"Key": "Name", "Value": name}]}],
    )["Instances"][0]


def _deny(connection_cls, monkeypatch, service: str, operation: str, code: str = "AccessDenied"):
    original = connection_cls.client

    def client(self, svc):
        c = original(self, svc)

        def raise_error(**kwargs):
            raise ClientError({"Error": {"Code": code, "Message": "denied"}}, operation)

        c.meta.events.register(f"before-call.{service}.{operation}", raise_error)
        return c

    monkeypatch.setattr(connection_cls, "client", client)


@pytest.fixture
def aws():
    with mock_aws():
        connection = connect(region="ap-south-1")
        yield connection


def test_every_snapshot_file_is_produced(aws):
    ec2 = aws.client("ec2")
    _, subnet, sg = _network(ec2)
    _instance(ec2, subnet, sg)

    files = collect(aws, CollectContext(plugin="compute"))

    assert set(COMPUTE_SNAPSHOT_FILES) <= set(files)


def test_instances_are_flattened_not_reservation_shaped(aws):
    ec2 = aws.client("ec2")
    _, subnet, sg = _network(ec2)
    _instance(ec2, subnet, sg, name="a")
    _instance(ec2, subnet, sg, name="b")

    files = collect(aws, CollectContext(plugin="compute"))
    instances = files["compute.instances"]["Instances"]

    assert len(instances) == 2
    assert all("InstanceId" in item for item in instances)
    assert all("Instances" not in item for item in instances)


def test_instances_are_sorted_by_id(aws):
    ec2 = aws.client("ec2")
    _, subnet, sg = _network(ec2)
    _instance(ec2, subnet, sg, name="a")
    _instance(ec2, subnet, sg, name="b")

    files = collect(aws, CollectContext(plugin="compute"))
    ids = [item["InstanceId"] for item in files["compute.instances"]["Instances"]]

    assert ids == sorted(ids)


def test_instance_attributes_are_keyed_by_instance_with_base64_user_data(aws):
    ec2 = aws.client("ec2")
    _, subnet, sg = _network(ec2)
    instance = _instance(ec2, subnet, sg)

    files = collect(aws, CollectContext(plugin="compute"))
    attributes = files["compute.instance_attributes"][instance["InstanceId"]]

    assert "disableApiTermination" in attributes
    assert base64.b64decode(attributes["userData"]).decode() == USER_DATA


def test_images_are_requested_for_the_amis_in_use(aws):
    ec2 = aws.client("ec2")
    _, subnet, sg = _network(ec2)
    _instance(ec2, subnet, sg)

    files = collect(aws, CollectContext(plugin="compute"))
    image_ids = [image["ImageId"] for image in files["compute.images"]["Images"]]

    assert AMI in image_ids


class _FakeImages:
    """Rejects a known-bad id the way EC2 does, then succeeds on the retry."""

    def __init__(self) -> None:
        self.requests: list[list[str]] = []

    def can_paginate(self, method: str) -> bool:
        return False

    def describe_images(self, ImageIds):  # noqa: N803 - boto3 spelling
        self.requests.append(list(ImageIds))
        if "ami-deadbeef" in ImageIds:
            raise ClientError(
                {
                    "Error": {
                        "Code": "InvalidAMIID.NotFound",
                        "Message": "The image id '[ami-deadbeef]' does not exist",
                    }
                },
                "DescribeImages",
            )
        return {"Images": [{"ImageId": image_id} for image_id in ImageIds]}


def test_unknown_ami_is_dropped_and_the_rest_still_come_back():
    client = _FakeImages()
    instances = [
        {"InstanceId": "i-1", "ImageId": AMI},
        {"InstanceId": "i-2", "ImageId": "ami-deadbeef"},
    ]

    images = _collect_images(client, instances, CollectContext(plugin="compute"))

    assert client.requests[0] == sorted([AMI, "ami-deadbeef"])
    assert client.requests[-1] == [AMI]
    assert [image["ImageId"] for image in images] == [AMI]


def test_key_pairs_are_requested_with_public_key_material(aws):
    ec2 = aws.client("ec2")
    ec2.create_key_pair(KeyName="web-key")
    _, subnet, sg = _network(ec2)
    _instance(ec2, subnet, sg)

    calls: list[dict] = []
    original_client = type(aws).client
    real = ec2.describe_key_pairs

    def client(self, svc):
        c = original_client(self, svc)
        if svc == "ec2":

            def describe_key_pairs(**kwargs):
                calls.append(kwargs)
                return real(**kwargs)

            c.describe_key_pairs = describe_key_pairs
        return c

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(type(aws), "client", client)
        collect(aws, CollectContext(plugin="compute"))

    assert calls and calls[0].get("IncludePublicKey") is True


def test_launch_template_versions_are_keyed_by_template(aws):
    ec2 = aws.client("ec2")
    _, subnet, sg = _network(ec2)
    template = ec2.create_launch_template(
        LaunchTemplateName="web-lt",
        LaunchTemplateData={"ImageId": AMI, "InstanceType": "t3.micro"},
    )["LaunchTemplate"]
    ec2.create_launch_template_version(
        LaunchTemplateId=template["LaunchTemplateId"],
        LaunchTemplateData={"ImageId": AMI, "InstanceType": "t3.small"},
    )

    files = collect(aws, CollectContext(plugin="compute"))
    entry = files["compute.launch_templates"][template["LaunchTemplateId"]]

    assert entry["template"]["LaunchTemplateName"] == "web-lt"
    assert len(entry["versions"]) == 2


def test_autoscaling_groups_and_policies_are_collected(aws):
    ec2 = aws.client("ec2")
    _, subnet, sg = _network(ec2)
    template = ec2.create_launch_template(
        LaunchTemplateName="web-lt",
        LaunchTemplateData={"ImageId": AMI, "InstanceType": "t3.micro"},
    )["LaunchTemplate"]
    autoscaling = aws.client("autoscaling")
    autoscaling.create_auto_scaling_group(
        AutoScalingGroupName="web-asg",
        MinSize=1,
        MaxSize=3,
        DesiredCapacity=2,
        VPCZoneIdentifier=subnet,
        LaunchTemplate={"LaunchTemplateId": template["LaunchTemplateId"], "Version": "1"},
    )
    autoscaling.put_scaling_policy(
        AutoScalingGroupName="web-asg",
        PolicyName="cpu",
        PolicyType="TargetTrackingScaling",
        TargetTrackingConfiguration={
            "PredefinedMetricSpecification": {"PredefinedMetricType": "ASGAverageCPUUtilization"},
            "TargetValue": 60.0,
        },
    )

    files = collect(aws, CollectContext(plugin="compute"))

    groups = files["compute.autoscaling_groups"]["AutoScalingGroups"]
    policies = files["compute.scaling_policies"]["ScalingPolicies"]
    assert [group["AutoScalingGroupName"] for group in groups] == ["web-asg"]
    assert [policy["PolicyName"] for policy in policies] == ["cpu"]


def test_access_denied_on_instances_records_a_finding_and_keeps_other_files(aws, monkeypatch):
    ec2 = aws.client("ec2")
    _network(ec2)
    _deny(type(aws), monkeypatch, "ec2", "DescribeInstances")

    ctx = CollectContext(plugin="compute")
    files = collect(aws, ctx)

    assert [f.suggestion for f in ctx.findings] == ["Grant ec2:DescribeInstances"]
    assert files["compute.instances"] == {"Instances": []}
    assert "compute.addresses" in files


def test_access_denied_on_scaling_policies_leaves_groups_populated(aws, monkeypatch):
    ec2 = aws.client("ec2")
    _, subnet, sg = _network(ec2)
    template = ec2.create_launch_template(
        LaunchTemplateName="web-lt",
        LaunchTemplateData={"ImageId": AMI, "InstanceType": "t3.micro"},
    )["LaunchTemplate"]
    aws.client("autoscaling").create_auto_scaling_group(
        AutoScalingGroupName="web-asg",
        MinSize=1,
        MaxSize=3,
        DesiredCapacity=2,
        VPCZoneIdentifier=subnet,
        LaunchTemplate={"LaunchTemplateId": template["LaunchTemplateId"], "Version": "1"},
    )
    _deny(type(aws), monkeypatch, "autoscaling", "DescribePolicies")

    ctx = CollectContext(plugin="compute")
    files = collect(aws, ctx)

    assert files["compute.autoscaling_groups"]["AutoScalingGroups"]
    assert files["compute.scaling_policies"] == {"ScalingPolicies": []}
    assert any("autoscaling:DescribePolicies" in (f.suggestion or "") for f in ctx.findings)


def test_no_describe_tags_call_is_made(aws, monkeypatch):
    ec2 = aws.client("ec2")
    _, subnet, sg = _network(ec2)
    _instance(ec2, subnet, sg)

    calls: list[str] = []
    original_client = type(aws).client

    def client(self, svc):
        c = original_client(self, svc)
        if svc == "ec2":
            real = c.describe_tags

            def describe_tags(**kwargs):
                calls.append("describe_tags")
                return real(**kwargs)

            c.describe_tags = describe_tags
        return c

    monkeypatch.setattr(type(aws), "client", client)
    collect(aws, CollectContext(plugin="compute"))

    assert calls == []


def test_source_module_exposes_access_denied_helper():
    assert hasattr(source, "is_access_denied")
