"""Hand-built AWS response shapes for compute normalizer tests (spec 4 §4.2)."""

from __future__ import annotations

import base64
from typing import Any

AMI = "ami-1111"


def tags(**pairs: str) -> list[dict[str, str]]:
    return [{"Key": key, "Value": value} for key, value in pairs.items()]


def instance(
    instance_id: str = "i-1",
    *,
    name: str | None = "web",
    instance_type: str = "t3.micro",
    state: str = "running",
    zone: str = "ap-south-1a",
    zone_id: str | None = "aps1-az1",
    subnet_id: str = "subnet-1",
    security_groups: list[str] | None = None,
    image_id: str = AMI,
    architecture: str = "x86_64",
    private_ip: str | None = "10.0.1.10",
    public_ip: str | None = None,
    root_device: str = "/dev/xvda",
    block_devices: list[tuple[str, str]] | None = None,
    instance_store_devices: list[str] | None = None,
    key_name: str | None = None,
    iam_profile: str | None = None,
    tenancy: str = "default",
    placement_group: str | None = None,
    lifecycle: str | None = None,
    monitoring: str = "disabled",
    imds: dict[str, Any] | None = None,
    extra_interfaces: list[str] | None = None,
    extra_tags: dict[str, str] | None = None,
) -> dict[str, Any]:
    mappings = [
        {"DeviceName": device, "Ebs": {"VolumeId": volume}}
        for device, volume in (block_devices or [(root_device, "vol-1")])
    ]
    mappings += [{"DeviceName": device} for device in instance_store_devices or []]

    all_tags = {"Name": name} if name else {}
    all_tags.update(extra_tags or {})

    interfaces = [{"NetworkInterfaceId": "eni-1", "SubnetId": subnet_id}]
    interfaces += [
        {"NetworkInterfaceId": eni, "SubnetId": subnet_id} for eni in extra_interfaces or []
    ]

    payload: dict[str, Any] = {
        "InstanceId": instance_id,
        "InstanceType": instance_type,
        "Architecture": architecture,
        "State": {"Name": state},
        "Placement": {
            "AvailabilityZone": zone,
            "AvailabilityZoneId": zone_id,
            "Tenancy": tenancy,
        },
        "SubnetId": subnet_id,
        "SecurityGroups": [{"GroupId": sg} for sg in security_groups or []],
        "ImageId": image_id,
        "RootDeviceName": root_device,
        "BlockDeviceMappings": mappings,
        "NetworkInterfaces": interfaces,
        "Monitoring": {"State": monitoring},
        "Tags": [{"Key": k, "Value": v} for k, v in all_tags.items()],
    }
    if private_ip:
        payload["PrivateIpAddress"] = private_ip
    if public_ip:
        payload["PublicIpAddress"] = public_ip
    if key_name:
        payload["KeyName"] = key_name
    if iam_profile:
        payload["IamInstanceProfile"] = {"Arn": iam_profile}
    if placement_group:
        payload["Placement"]["GroupName"] = placement_group
    if lifecycle:
        payload["InstanceLifecycle"] = lifecycle
    if imds:
        payload["MetadataOptions"] = imds
    return payload


def volume(
    volume_id: str = "vol-1",
    *,
    size: int = 20,
    volume_type: str = "gp3",
    zone: str = "ap-south-1a",
    iops: int | None = None,
    throughput: int | None = None,
    encrypted: bool = False,
    kms_key: str | None = None,
    attachments: list[str] | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "VolumeId": volume_id,
        "Size": size,
        "VolumeType": volume_type,
        "AvailabilityZone": zone,
        "Encrypted": encrypted,
        "Attachments": [{"InstanceId": item} for item in attachments or ["i-1"]],
        "Tags": tags(Name=name) if name else [],
    }
    if iops is not None:
        payload["Iops"] = iops
    if throughput is not None:
        payload["Throughput"] = throughput
    if kms_key:
        payload["KmsKeyId"] = kms_key
    return payload


def address(
    public_ip: str = "13.1.1.1",
    *,
    allocation_id: str = "eipalloc-1",
    instance_id: str | None = "i-1",
    domain: str = "vpc",
    name: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "PublicIp": public_ip,
        "AllocationId": allocation_id,
        "Domain": domain,
        "Tags": tags(Name=name) if name else [],
    }
    if instance_id:
        payload["InstanceId"] = instance_id
    return payload


def snapshot_files(
    *,
    instances: list[dict[str, Any]] | None = None,
    volumes: list[dict[str, Any]] | None = None,
    addresses: list[dict[str, Any]] | None = None,
    images: list[dict[str, Any]] | None = None,
    key_pairs: list[dict[str, Any]] | None = None,
    instance_types: list[dict[str, Any]] | None = None,
    launch_templates: dict[str, Any] | None = None,
    autoscaling_groups: list[dict[str, Any]] | None = None,
    scaling_policies: list[dict[str, Any]] | None = None,
    user_data: dict[str, str] | None = None,
    termination_protection: dict[str, bool] | None = None,
) -> dict[str, Any]:
    instances = instances if instances is not None else [instance()]
    volumes = volumes if volumes is not None else [volume()]
    attributes = {}
    for item in instances:
        raw = (user_data or {}).get(item["InstanceId"])
        attributes[item["InstanceId"]] = {
            "userData": base64.b64encode(raw.encode()).decode() if raw else None,
            "disableApiTermination": (termination_protection or {}).get(item["InstanceId"], False),
        }
    return {
        "compute.instances": {"Instances": instances},
        "compute.instance_attributes": attributes,
        "compute.volumes": {"Volumes": volumes},
        "compute.addresses": {"Addresses": addresses or []},
        "compute.images": {
            "Images": images
            if images is not None
            else [{"ImageId": AMI, "PlatformDetails": "Linux/UNIX", "Name": "amzn2-x86_64"}]
        },
        "compute.key_pairs": {"KeyPairs": key_pairs or []},
        "compute.instance_types": {"InstanceTypes": instance_types or []},
        "compute.launch_templates": launch_templates or {},
        "compute.autoscaling_groups": {"AutoScalingGroups": autoscaling_groups or []},
        "compute.scaling_policies": {"ScalingPolicies": scaling_policies or []},
    }
