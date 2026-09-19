"""Build the eight compute golden fixtures from the test builders (spec 4 plan 1 §Tasks).

    uv run python scripts/make_compute_fixtures.py

Like the database fixtures and unlike the network ones, these are not anonymized moto
captures: EC2/VPC identifiers here are plain readable strings we chose ourselves, so there is
nothing to anonymize. Each case builds `network.*` files (flattened lists, network collector
shape) and `compute.*` files (raw-boto3 shape, compute collector shape) and writes them to
`tests/plugins/compute/fixtures/<case>/snapshot/`.

Then run `uv run pytest --update-golden tests/plugins/compute/test_golden.py` and review the
expected_ir.json diffs.

Plans 3-5 assert difference keys and golden HCL against these exact case names, so the set is
fixed here rather than grown ad hoc.
"""

from __future__ import annotations

import base64
import json
import shutil
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.plugins.compute import aws_builders as vm  # noqa: E402
from tests.plugins.network import aws_builders as net  # noqa: E402

FIXTURES = ROOT / "tests" / "plugins" / "compute" / "fixtures"

VPC = "vpc-fixture"
SUBNET_A = "subnet-a"
SUBNET_B = "subnet-b"
SG_WEB = "sg-web"

Files = dict[str, Any]


def _site(*, sg_specs: list[tuple[str, str]] | None = None) -> Files:
    """A VPC with two public subnets across two zones, plus security groups."""
    sg_specs = sg_specs or [(SG_WEB, "web-sg")]
    return {
        "network.vpcs": [net.vpc(VPC, "10.0.0.0/16", name="migration-test-vpc")],
        "network.subnets": [
            net.subnet(SUBNET_A, VPC, "10.0.1.0/24", zone="ap-south-1a", name="subnet-1"),
            net.subnet(SUBNET_B, VPC, "10.0.2.0/24", zone="ap-south-1b", name="subnet-2"),
        ],
        "network.vpc_attributes": {VPC: {"enableDnsHostnames": True, "enableDnsSupport": True}},
        "network.route_tables": [
            net.route_table(
                f"rtb-{VPC}",
                VPC,
                subnets=(SUBNET_A, SUBNET_B),
                main=True,
                routes=[net.route("0.0.0.0/0", GatewayId="igw-fixture")],
            )
        ],
        "network.internet_gateways": [
            {"InternetGatewayId": "igw-fixture", "Attachments": [{"VpcId": VPC}]}
        ],
        "network.security_groups": [
            net.security_group(sg_id, VPC, sg_name, name=sg_name) for sg_id, sg_name in sg_specs
        ],
    }


def _compute(**overrides: Any) -> Files:
    files = vm.snapshot_files(**overrides)
    return {name: payload for name, payload in files.items()}


def standalone_linux() -> Files:
    """The testbed shape: one boot volume, a key pair, user data and an Elastic IP."""
    return {
        **_site(),
        **_compute(
            instances=[
                vm.instance(
                    "i-web",
                    name="web",
                    subnet_id=SUBNET_A,
                    security_groups=[SG_WEB],
                    key_name="web-key",
                    public_ip="13.1.1.1",
                    block_devices=[("/dev/xvda", "vol-web-root")],
                )
            ],
            volumes=[vm.volume("vol-web-root", size=20, volume_type="gp3", name="web-root")],
            addresses=[vm.address("13.1.1.1", instance_id="i-web", name="web-eip")],
            key_pairs=[{"KeyName": "web-key", "PublicKey": "ssh-rsa AAAAB3NzaC1 web-key"}],
            user_data={"i-web": "#!/bin/bash\necho hello\n"},
            instance_types=[_type("t3.micro", 2, 1024)],
        ),
    }


def stopped_instance() -> Files:
    return {
        **_site(),
        **_compute(
            instances=[
                vm.instance(
                    "i-batch",
                    name="batch",
                    state="stopped",
                    subnet_id=SUBNET_B,
                    zone="ap-south-1b",
                    zone_id="aps1-az2",
                    security_groups=[SG_WEB],
                    block_devices=[("/dev/xvda", "vol-batch-root")],
                )
            ],
            volumes=[vm.volume("vol-batch-root", size=30, volume_type="gp2", zone="ap-south-1b")],
            instance_types=[_type("t3.micro", 2, 1024)],
        ),
    }


def extra_volumes() -> Files:
    """gp3 with provisioned IOPS and throughput, plus an ephemeral instance-store volume."""
    return {
        **_site(),
        **_compute(
            instances=[
                vm.instance(
                    "i-data",
                    name="data",
                    instance_type="m5.xlarge",
                    subnet_id=SUBNET_A,
                    security_groups=[SG_WEB],
                    block_devices=[
                        ("/dev/xvda", "vol-data-root"),
                        ("/dev/xvdb", "vol-data-1"),
                    ],
                    instance_store_devices=["/dev/xvdc"],
                )
            ],
            volumes=[
                vm.volume("vol-data-root", size=20, volume_type="gp3"),
                vm.volume(
                    "vol-data-1",
                    size=500,
                    volume_type="gp3",
                    iops=6000,
                    throughput=250,
                    encrypted=True,
                    kms_key="arn:aws:kms:ap-south-1:123456789012:key/data",
                    name="data-1",
                ),
            ],
            instance_types=[_type("m5.xlarge", 4, 16384)],
        ),
    }


def windows_instance() -> Files:
    return {
        **_site(),
        **_compute(
            instances=[
                vm.instance(
                    "i-win",
                    name="win",
                    instance_type="t3.large",
                    subnet_id=SUBNET_A,
                    security_groups=[SG_WEB],
                    block_devices=[("/dev/sda1", "vol-win-root")],
                    root_device="/dev/sda1",
                )
            ],
            volumes=[vm.volume("vol-win-root", size=50, volume_type="gp2")],
            images=[
                {
                    "ImageId": vm.AMI,
                    "PlatformDetails": "Windows",
                    "Platform": "windows",
                    "Name": "Windows_Server-2022-English-Full-Base",
                }
            ],
            instance_types=[_type("t3.large", 2, 8192)],
        ),
    }


def spot_instance() -> Files:
    """Spot, dedicated tenancy, a placement group and IMDSv2 required — the awkward corners."""
    return {
        **_site(),
        **_compute(
            instances=[
                vm.instance(
                    "i-spot",
                    name="spot",
                    instance_type="c5.xlarge",
                    subnet_id=SUBNET_A,
                    security_groups=[SG_WEB],
                    lifecycle="spot",
                    tenancy="dedicated",
                    placement_group="cluster-1",
                    imds={"HttpTokens": "required", "HttpPutResponseHopLimit": 2},
                    monitoring="enabled",
                    iam_profile="arn:aws:iam::123456789012:instance-profile/app-role",
                    block_devices=[("/dev/xvda", "vol-spot-root")],
                )
            ],
            volumes=[vm.volume("vol-spot-root", size=20, volume_type="gp3")],
            termination_protection={"i-spot": True},
            instance_types=[_type("c5.xlarge", 4, 8192)],
        ),
    }


def eks_member() -> Files:
    """An EKS node group member: in the inventory, left to the EKS family (spec 4 §1)."""
    return {
        **_site(),
        **_compute(
            instances=[
                vm.instance(
                    "i-node",
                    name="eks-node",
                    subnet_id=SUBNET_A,
                    security_groups=[SG_WEB],
                    extra_tags={"eks:cluster-name": "test-eks-cluster"},
                    block_devices=[("/dev/xvda", "vol-node-root")],
                )
            ],
            volumes=[vm.volume("vol-node-root", size=20, volume_type="gp3")],
            instance_types=[_type("t3.micro", 2, 1024)],
        ),
    }


def asg_two_az() -> Files:
    """A launch template with two versions, an ASG across both zones, CPU target tracking."""
    data = {
        "ImageId": vm.AMI,
        "InstanceType": "t3.small",
        "SecurityGroupIds": [SG_WEB],
        "KeyName": "web-key",
        "UserData": base64.b64encode(b"#!/bin/bash\necho asg\n").decode(),
    }
    return {
        **_site(),
        **_compute(
            instances=[],
            volumes=[],
            key_pairs=[{"KeyName": "web-key", "PublicKey": "ssh-rsa AAAAB3NzaC1 web-key"}],
            instance_types=[_type("t3.small", 2, 2048)],
            launch_templates={
                "lt-web": {
                    "template": {
                        "LaunchTemplateId": "lt-web",
                        "LaunchTemplateName": "web-lt",
                        "DefaultVersionNumber": 1,
                    },
                    "versions": [
                        {"VersionNumber": 1, "LaunchTemplateData": data},
                        {
                            "VersionNumber": 2,
                            "LaunchTemplateData": {**data, "InstanceType": "t3.medium"},
                        },
                    ],
                }
            },
            autoscaling_groups=[
                {
                    "AutoScalingGroupName": "web-asg",
                    "MinSize": 2,
                    "MaxSize": 6,
                    "DesiredCapacity": 3,
                    "AvailabilityZones": ["ap-south-1a", "ap-south-1b"],
                    "VPCZoneIdentifier": f"{SUBNET_A},{SUBNET_B}",
                    "HealthCheckType": "ELB",
                    "HealthCheckGracePeriod": 300,
                    "TargetGroupARNs": [
                        "arn:aws:elasticloadbalancing:ap-south-1:123456789012:targetgroup/web/1"
                    ],
                    "LaunchTemplate": {"LaunchTemplateId": "lt-web", "Version": "1"},
                    "Tags": [],
                }
            ],
            scaling_policies=[
                {
                    "AutoScalingGroupName": "web-asg",
                    "PolicyName": "cpu",
                    "PolicyType": "TargetTrackingScaling",
                    "TargetTrackingConfiguration": {
                        "PredefinedMetricSpecification": {
                            "PredefinedMetricType": "ASGAverageCPUUtilization"
                        },
                        "TargetValue": 60.0,
                    },
                }
            ],
        ),
    }


def small_boot_disk() -> Files:
    """An 8 GiB Amazon Linux root volume, which no stock GCP image will fit on.

    Added after the 2026-09-19 live run, where this exact shape failed at apply with
    "Requested disk size cannot be smaller than the image size (20 GB)". Every other fixture
    had a boot disk of 20 GiB or more, which is why nothing caught it.
    """
    return {
        **_site(),
        **_compute(
            instances=[
                vm.instance(
                    "i-small",
                    name="small",
                    subnet_id=SUBNET_A,
                    security_groups=[SG_WEB],
                    block_devices=[("/dev/xvda", "vol-small-root")],
                )
            ],
            volumes=[vm.volume("vol-small-root", size=8, volume_type="gp3")],
            instance_types=[_type("t3.micro", 2, 1024)],
        ),
    }


def unsupported_resources() -> Files:
    """A Dedicated Host and a Spot Fleet request: reported, never translated (spec 4 §4.4)."""
    files = {
        **_site(),
        **_compute(instances=[], volumes=[]),
    }
    files["compute.hosts"] = {"Hosts": [{"HostId": "h-dedicated"}]}
    files["compute.spot_fleet_requests"] = {
        "SpotFleetRequestConfigs": [{"SpotFleetRequestId": "sfr-batch"}]
    }
    return files


def _type(name: str, vcpus: int, memory_mib: int) -> dict[str, Any]:
    return {
        "InstanceType": name,
        "VCpuInfo": {"DefaultVCpus": vcpus},
        "MemoryInfo": {"SizeInMiB": memory_mib},
    }


CASES = {
    "asg_two_az": asg_two_az,
    "eks_member": eks_member,
    "extra_volumes": extra_volumes,
    "small_boot_disk": small_boot_disk,
    "spot_instance": spot_instance,
    "standalone_linux": standalone_linux,
    "stopped_instance": stopped_instance,
    "unsupported_resources": unsupported_resources,
    "windows_instance": windows_instance,
}


def main() -> None:
    for case, build in CASES.items():
        snapshot_dir = FIXTURES / case / "snapshot"
        if snapshot_dir.exists():
            shutil.rmtree(snapshot_dir)
        snapshot_dir.mkdir(parents=True)
        for name, payload in build().items():
            (snapshot_dir / f"{name}.json").write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n"
            )
        print(f"wrote {case}")


if __name__ == "__main__":
    main()
