"""Compute normalization (spec 4 §4.2, §4.3). Faithful capture only."""

from __future__ import annotations

from typing import Any

import pytest

from easymigrate.core.plugin import NormalizeContext, Snapshot
from easymigrate.ir.base import IntentObject, SourceRef
from easymigrate.ir.compute import ComputeInstance, Disk, ExternalAddress
from easymigrate.ir.network import FirewallPolicy, Network, Subnet
from easymigrate.plugins.compute.normalize import normalize
from tests.plugins.compute import aws_builders as b


def _network_objects() -> list[IntentObject]:
    def source(type_: str, native: str) -> SourceRef:
        return SourceRef(cloud="aws", type=type_, id=native, region="ap-south-1")

    return [
        Network(
            id="network:vpc",
            name="vpc",
            source=source("vpc", "vpc-1"),
            cidr_blocks=["10.0.0.0/16"],
            dns_hostnames=True,
            dns_resolution=True,
        ),
        Subnet(
            id="subnet:public-1",
            name="public-1",
            source=source("subnet", "subnet-1"),
            network="network:vpc",
            cidr="10.0.1.0/24",
            zone="ap-south-1a",
            internet="public",
            map_public_ip=True,
        ),
        Subnet(
            id="subnet:public-2",
            name="public-2",
            source=source("subnet", "subnet-2"),
            network="network:vpc",
            cidr="10.0.2.0/24",
            zone="ap-south-1b",
            internet="public",
            map_public_ip=True,
        ),
        FirewallPolicy(
            id="firewall_policy:web-sg",
            name="web-sg",
            source=source("security-group", "sg-1"),
            network="network:vpc",
        ),
    ]


def run(
    files: dict[str, Any], *, strict: bool = False
) -> tuple[list[IntentObject], NormalizeContext]:
    ctx = NormalizeContext(account_id="1", region="ap-south-1", strict=strict)
    ctx.objects = {obj.id: obj for obj in _network_objects()}
    produced = normalize(Snapshot(files), ctx)
    return produced, ctx


def codes(ctx: NormalizeContext) -> list[str]:
    return [finding.code for finding in ctx.findings]


def only(objects: list[IntentObject], cls: type) -> list[Any]:
    return [obj for obj in objects if isinstance(obj, cls)]


def test_standalone_instance_becomes_a_faithful_object():
    objects, ctx = run(b.snapshot_files())
    instance = only(objects, ComputeInstance)[0]

    assert instance.id == "compute_instance:web"
    assert instance.instance_type == "t3.micro"
    assert instance.state == "running"
    assert instance.zone == "ap-south-1a"
    assert instance.zone_id == "aps1-az1"
    assert instance.subnet == "subnet:public-1"
    assert instance.image == b.AMI
    assert instance.platform == "linux"
    assert instance.external_ip == "none"


def test_security_groups_resolve_to_forward_references():
    objects, _ = run(b.snapshot_files(instances=[b.instance(security_groups=["sg-1"])]))
    instance = only(objects, ComputeInstance)[0]

    assert instance.firewall_policies == ["firewall_policy:web-sg"]


def test_policy_membership_comes_from_the_instance_not_the_policy():
    """Spec 4 §2.1: the instance points at the policy; the policy holds no back-reference."""
    from easymigrate.differences.membership import policy_members
    from easymigrate.ir.base import IRDocument

    objects, _ = run(b.snapshot_files(instances=[b.instance(security_groups=["sg-1"])]))
    doc = IRDocument(
        source={"cloud": "aws", "account_id": "1", "region": "ap-south-1"},
        objects=[*_network_objects(), *objects],
    )

    assert policy_members(doc)["firewall_policy:web-sg"] == {"compute_instance:web"}


def test_boot_disk_is_first_and_extras_follow_sorted_by_device():
    files = b.snapshot_files(
        instances=[
            b.instance(
                block_devices=[
                    ("/dev/xvdf", "vol-data2"),
                    ("/dev/xvda", "vol-root"),
                    ("/dev/xvdb", "vol-data1"),
                ]
            )
        ],
        volumes=[
            b.volume("vol-root"),
            b.volume("vol-data1", volume_type="gp2"),
            b.volume("vol-data2", volume_type="gp2"),
        ],
    )
    objects, _ = run(files)
    instance = only(objects, ComputeInstance)[0]
    disks = {disk.id: disk for disk in only(objects, Disk)}

    assert disks[instance.disks[0]].boot is True
    assert len(instance.disks) == 3
    assert [disks[d].source.id for d in instance.disks] == ["vol-root", "vol-data1", "vol-data2"]


def test_instance_store_volume_has_no_type_or_size():
    files = b.snapshot_files(instances=[b.instance(instance_store_devices=["/dev/xvdc"])])
    objects, _ = run(files)
    scratch = [disk for disk in only(objects, Disk) if disk.instance_store]

    assert len(scratch) == 1
    assert scratch[0].type is None
    assert scratch[0].size_gib is None


def test_provisioned_iops_volume_is_captured():
    files = b.snapshot_files(
        volumes=[b.volume("vol-1", volume_type="gp3", iops=6000, throughput=250)]
    )
    objects, _ = run(files)
    disk = only(objects, Disk)[0]

    assert (disk.type, disk.iops, disk.throughput_mibps) == ("gp3", 6000, 250)


def test_stopped_instance_is_translated():
    objects, _ = run(b.snapshot_files(instances=[b.instance(state="stopped")]))
    assert only(objects, ComputeInstance)[0].state == "stopped"


@pytest.mark.parametrize("state", ["pending", "shutting-down", "stopping", "terminated"])
def test_transient_states_are_reported_not_translated(state: str):
    objects, ctx = run(b.snapshot_files(instances=[b.instance(state=state)]))

    assert only(objects, ComputeInstance) == []
    assert "VM-STATE-TRANSIENT" in codes(ctx)


def test_elastic_ip_attached_to_an_instance_becomes_a_reference():
    files = b.snapshot_files(addresses=[b.address(instance_id="i-1", name="web-eip")])
    objects, _ = run(files)
    instance = only(objects, ComputeInstance)[0]
    addresses = only(objects, ExternalAddress)

    assert addresses[0].attached is True
    assert instance.external_ip == addresses[0].id


def test_unattached_elastic_ip_is_still_recorded():
    files = b.snapshot_files(addresses=[b.address(instance_id=None, name="spare")])
    objects, _ = run(files)

    assert only(objects, ExternalAddress)[0].attached is False
    assert only(objects, ComputeInstance)[0].external_ip == "none"


def test_public_ip_without_an_elastic_ip_is_ephemeral():
    objects, _ = run(b.snapshot_files(instances=[b.instance(public_ip="13.2.2.2")]))
    assert only(objects, ComputeInstance)[0].external_ip == "ephemeral"


def test_sizing_comes_from_the_instance_types_file():
    files = b.snapshot_files(
        instances=[b.instance(instance_type="m5.xlarge")],
        instance_types=[
            {
                "InstanceType": "m5.xlarge",
                "VCpuInfo": {"DefaultVCpus": 4},
                "MemoryInfo": {"SizeInMiB": 16384},
            }
        ],
    )
    objects, _ = run(files)
    instance = only(objects, ComputeInstance)[0]

    assert (instance.vcpus, instance.memory_mib) == (4, 16384)


def test_sizing_falls_back_to_the_table():
    objects, _ = run(b.snapshot_files(instances=[b.instance(instance_type="t3.micro")]))
    instance = only(objects, ComputeInstance)[0]

    assert (instance.vcpus, instance.memory_mib) == (2, 1024)


def test_unknown_sizing_is_reported():
    objects, ctx = run(b.snapshot_files(instances=[b.instance(instance_type="zz.mega")]))
    instance = only(objects, ComputeInstance)[0]

    assert (instance.vcpus, instance.memory_mib) == (None, None)
    assert "SCAN-VM-TYPE-UNKNOWN" in codes(ctx)


def test_user_data_is_decoded():
    files = b.snapshot_files(user_data={"i-1": "#!/bin/bash\necho hi\n"})
    objects, _ = run(files)

    assert only(objects, ComputeInstance)[0].user_data == "#!/bin/bash\necho hi\n"


def test_binary_user_data_is_reported_not_guessed():
    files = b.snapshot_files()
    files["compute.instance_attributes"]["i-1"]["userData"] = "//5oAGkA"  # UTF-16 bytes
    objects, ctx = run(files)

    assert only(objects, ComputeInstance)[0].user_data is None
    assert "VM-USER-DATA-BINARY" in codes(ctx)


def test_public_key_material_is_carried_when_aws_returns_it():
    files = b.snapshot_files(
        instances=[b.instance(key_name="web-key")],
        key_pairs=[{"KeyName": "web-key", "PublicKey": "ssh-rsa AAAA web"}],
    )
    objects, _ = run(files)
    instance = only(objects, ComputeInstance)[0]

    assert instance.key_pair == "web-key"
    assert instance.key_material == "ssh-rsa AAAA web"


def test_missing_key_material_is_never_invented():
    files = b.snapshot_files(
        instances=[b.instance(key_name="web-key")],
        key_pairs=[{"KeyName": "web-key"}],
    )
    objects, _ = run(files)

    assert only(objects, ComputeInstance)[0].key_material is None


def test_eks_member_is_kept_in_the_inventory_and_flagged():
    files = b.snapshot_files(
        instances=[b.instance(extra_tags={"eks:cluster-name": "test-eks-cluster"})]
    )
    objects, ctx = run(files)
    instance = only(objects, ComputeInstance)[0]

    assert instance.eks_cluster == "test-eks-cluster"
    assert "VM-EKS-MEMBER" in codes(ctx)


def test_multi_nic_instance_uses_the_primary_subnet_and_is_flagged():
    files = b.snapshot_files(instances=[b.instance(extra_interfaces=["eni-2"])])
    objects, ctx = run(files)

    assert only(objects, ComputeInstance)[0].subnet == "subnet:public-1"
    assert "VM-MULTI-NIC" in codes(ctx)


def test_unresolvable_subnet_drops_the_instance_with_a_scan_gap():
    files = b.snapshot_files(instances=[b.instance(subnet_id="subnet-absent")])
    objects, ctx = run(files)

    assert only(objects, ComputeInstance) == []
    assert any(f.level == "scan_gap" for f in ctx.findings)


def test_unresolvable_security_group_is_omitted_with_a_scan_gap():
    files = b.snapshot_files(instances=[b.instance(security_groups=["sg-1", "sg-absent"])])
    objects, ctx = run(files)

    assert only(objects, ComputeInstance)[0].firewall_policies == ["firewall_policy:web-sg"]
    assert any(f.level == "scan_gap" for f in ctx.findings)


def test_spot_tenancy_placement_and_imds_are_captured():
    files = b.snapshot_files(
        instances=[
            b.instance(
                lifecycle="spot",
                tenancy="dedicated",
                placement_group="cluster-1",
                imds={"HttpTokens": "required", "HttpPutResponseHopLimit": 2},
                monitoring="enabled",
            )
        ],
        termination_protection={"i-1": True},
    )
    objects, _ = run(files)
    instance = only(objects, ComputeInstance)[0]

    assert instance.spot is True
    assert instance.tenancy == "dedicated"
    assert instance.placement_group == "cluster-1"
    assert instance.imds is not None and instance.imds.http_tokens == "required"
    assert instance.imds.hop_limit == 2
    assert instance.monitoring is True
    assert instance.termination_protection is True


def _asg_files() -> dict[str, Any]:
    template_data = {
        "ImageId": b.AMI,
        "InstanceType": "t3.micro",
        "SecurityGroupIds": ["sg-1"],
    }
    return b.snapshot_files(
        instances=[],
        volumes=[],
        launch_templates={
            "lt-1": {
                "template": {
                    "LaunchTemplateId": "lt-1",
                    "LaunchTemplateName": "web-lt",
                    "DefaultVersionNumber": 1,
                },
                "versions": [
                    {"VersionNumber": 1, "LaunchTemplateData": template_data},
                    {"VersionNumber": 2, "LaunchTemplateData": template_data},
                ],
            }
        },
        autoscaling_groups=[
            {
                "AutoScalingGroupName": "web-asg",
                "MinSize": 1,
                "MaxSize": 4,
                "DesiredCapacity": 2,
                "AvailabilityZones": ["ap-south-1a", "ap-south-1b"],
                "VPCZoneIdentifier": "subnet-1,subnet-2",
                "HealthCheckType": "ELB",
                "HealthCheckGracePeriod": 300,
                "TargetGroupARNs": ["arn:aws:elasticloadbalancing:::targetgroup/web/1"],
                "LaunchTemplate": {"LaunchTemplateId": "lt-1", "Version": "1"},
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
    )


def test_asg_becomes_an_instance_group_with_its_referenced_template():
    from easymigrate.ir.compute import InstanceGroup, InstanceTemplate

    objects, ctx = run(_asg_files())
    groups = only(objects, InstanceGroup)
    templates = only(objects, InstanceTemplate)

    assert len(groups) == 1
    assert len(templates) == 1, "only the referenced version becomes an object"
    assert templates[0].version == "1"
    assert groups[0].template == templates[0].id
    assert groups[0].subnets == ["subnet:public-1", "subnet:public-2"]
    assert groups[0].health_check_type == "ELB"
    assert groups[0].target_groups
    assert "VM-LT-VERSION-UNUSED" in codes(ctx)


def test_scaling_policy_is_attached_to_its_group():
    from easymigrate.ir.compute import InstanceGroup

    objects, _ = run(_asg_files())
    policy = only(objects, InstanceGroup)[0].scaling_policies[0]

    assert policy.policy_type == "TargetTrackingScaling"
    assert policy.metric == "ASGAverageCPUUtilization"
    assert policy.target_value == 60.0


@pytest.mark.parametrize(
    "file_name, key, payload, native",
    [
        ("compute.hosts", "Hosts", {"HostId": "h-1"}, "h-1"),
        (
            "compute.spot_fleet_requests",
            "SpotFleetRequestConfigs",
            {"SpotFleetRequestId": "sfr-1"},
            "sfr-1",
        ),
        ("compute.fleets", "Fleets", {"FleetId": "fleet-1"}, "fleet-1"),
        (
            "compute.capacity_reservations",
            "CapacityReservations",
            {"CapacityReservationId": "cr-1"},
            "cr-1",
        ),
    ],
)
def test_unsupported_resources_are_reported_with_no_objects(
    file_name: str, key: str, payload: dict, native: str
):
    """Spec 4 §4.4: named explicitly, because a user with these would otherwise assume the
    instances they govern migrated cleanly."""
    files = b.snapshot_files(instances=[], volumes=[])
    files[file_name] = {key: [payload]}
    objects, ctx = run(files)

    assert objects == []
    unsupported = [f for f in ctx.findings if f.level == "unsupported"]
    assert len(unsupported) == 1
    assert unsupported[0].code == "VM-RESOURCE-UNSUPPORTED"
    assert native in unsupported[0].message
    assert unsupported[0].suggestion


def test_normalizing_twice_is_identical():
    files = b.snapshot_files(
        instances=[b.instance(security_groups=["sg-1"])],
        addresses=[b.address()],
    )
    first, _ = run(files)
    second, _ = run(files)

    assert [obj.model_dump(mode="json") for obj in first] == [
        obj.model_dump(mode="json") for obj in second
    ]


def test_asg_member_instance_is_not_migrated_on_its_own():
    """Found by the 2026-09-22 live run: the MIG launches its own instances, so migrating the
    ASG's current members too would run every member twice."""
    files = _asg_files()
    files["compute.instances"] = {
        "Instances": [b.instance(extra_tags={"aws:autoscaling:groupName": "web-asg"})]
    }
    objects, ctx = run(files)

    assert only(objects, ComputeInstance) == []
    assert "VM-ASG-MEMBER" in codes(ctx)


def test_member_of_a_group_that_cannot_migrate_is_kept():
    """Review 2026-10-08: dropping the members of an unmigratable group lost both."""
    files = b.snapshot_files(
        instances=[b.instance(extra_tags={"aws:autoscaling:groupName": "legacy-asg"})]
    )
    objects, ctx = run(files)

    assert len(only(objects, ComputeInstance)) == 1
    assert "VM-ASG-MEMBER-KEPT" in codes(ctx)


def test_eks_member_is_still_captured_even_though_an_asg_launched_it():
    files = b.snapshot_files(
        instances=[
            b.instance(
                extra_tags={
                    "aws:autoscaling:groupName": "eks-nodes",
                    "eks:cluster-name": "prod",
                }
            )
        ]
    )
    objects, ctx = run(files)

    assert only(objects, ComputeInstance)[0].eks_cluster == "prod"
    assert "VM-ASG-MEMBER" not in codes(ctx)


def test_eks_node_group_asg_is_left_to_the_eks_family():
    from easymigrate.ir.compute import InstanceGroup, InstanceTemplate

    files = _asg_files()
    group = files["compute.autoscaling_groups"]["AutoScalingGroups"][0]
    group["Tags"] = [{"Key": "eks:cluster-name", "Value": "prod"}]
    objects, ctx = run(files)

    assert only(objects, InstanceGroup) == []
    assert only(objects, InstanceTemplate) == []
    assert "VM-ASG-EKS" in codes(ctx)


def test_self_managed_kubernetes_asg_is_left_to_the_kubernetes_family():
    from easymigrate.ir.compute import InstanceGroup

    files = _asg_files()
    group = files["compute.autoscaling_groups"]["AutoScalingGroups"][0]
    group["Tags"] = [{"Key": "kubernetes.io/cluster/prod", "Value": "owned"}]
    objects, ctx = run(files)

    assert only(objects, InstanceGroup) == []
    assert "VM-ASG-EKS" in codes(ctx)


def test_self_managed_kubernetes_node_is_captured_as_a_cluster_member():
    files = b.snapshot_files(
        instances=[
            b.instance(
                extra_tags={
                    "aws:autoscaling:groupName": "self-nodes",
                    "kubernetes.io/cluster/prod": "owned",
                }
            )
        ]
    )
    objects, ctx = run(files)

    assert only(objects, ComputeInstance)[0].eks_cluster == "prod"
    assert "VM-ASG-MEMBER" not in codes(ctx)


def _set_version(files, version, **template_extra):
    group = files["compute.autoscaling_groups"]["AutoScalingGroups"][0]
    group["LaunchTemplate"]["Version"] = version
    files["compute.launch_templates"]["lt-1"]["template"].update(template_extra)
    return files


@pytest.mark.parametrize(
    ("version", "extra", "expected"),
    [
        ("$Latest", {"LatestVersionNumber": 2}, "2"),
        ("$Default", {}, "1"),  # DefaultVersionNumber is 1 in the fixture
        (None, {}, "1"),
    ],
)
def test_symbolic_launch_template_versions_resolve(version, extra, expected):
    """Review 2026-10-08: groups on $Latest / $Default (the common case) never migrated."""
    from easymigrate.ir.compute import InstanceGroup, InstanceTemplate

    objects, ctx = run(_set_version(_asg_files(), version, **extra))
    assert len(only(objects, InstanceGroup)) == 1
    assert only(objects, InstanceTemplate)[0].version == expected


def test_mixed_instances_group_finds_its_template_under_the_policy():
    from easymigrate.ir.compute import InstanceGroup

    files = _asg_files()
    group = files["compute.autoscaling_groups"]["AutoScalingGroups"][0]
    spec = group.pop("LaunchTemplate")
    group["MixedInstancesPolicy"] = {
        "LaunchTemplate": {"LaunchTemplateSpecification": spec, "Overrides": []}
    }
    objects, _ = run(files)
    groups = only(objects, InstanceGroup)
    assert len(groups) == 1 and groups[0].mixed_instances


def test_ubuntu_ami_reported_as_linux_unix_is_recognised():
    """Review 2026-10-08: AWS reports Ubuntu AMIs as plain Linux/UNIX; they went to Rocky."""
    files = b.snapshot_files(instances=[b.instance()])
    files["compute.images"] = {
        "Images": [
            {
                "ImageId": b.AMI,
                "PlatformDetails": "Linux/UNIX",
                "Name": "ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-20240101",
            }
        ]
    }
    instance = only(run(files)[0], ComputeInstance)[0]
    assert "Ubuntu" in instance.platform_details


def test_unreadable_volume_is_reported_not_a_crash():
    files = b.snapshot_files(instances=[b.instance()], volumes=[])
    objects, ctx = run(files)
    assert len(only(objects, ComputeInstance)) == 1
    assert "VM-VOLUME-UNREADABLE" in codes(ctx)


def test_comma_separated_health_check_type_and_mac_architecture_load():
    from easymigrate.ir.compute import InstanceGroup

    files = _asg_files()
    files["compute.autoscaling_groups"]["AutoScalingGroups"][0]["HealthCheckType"] = "EC2,ELB"
    assert only(run(files)[0], InstanceGroup)[0].health_check_type == "ELB"
    files = b.snapshot_files(instances=[b.instance(architecture="x86_64_mac")])
    assert only(run(files)[0], ComputeInstance)[0].architecture == "x86_64"
