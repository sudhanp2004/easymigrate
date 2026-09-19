"""Collect raw AWS EC2 and Auto Scaling responses for one region (spec 4 §4.1, collector)."""

import re
from typing import Any

from easymigrate.core.plugin import CollectContext
from easymigrate.providers.aws.source import AwsConnection, is_access_denied

# (snapshot name, client, boto3 method, response list key, id field, IAM action)
_LISTS = (
    (
        "compute.volumes",
        "ec2",
        "describe_volumes",
        "Volumes",
        "VolumeId",
        "ec2:DescribeVolumes",
    ),
    (
        "compute.addresses",
        "ec2",
        "describe_addresses",
        "Addresses",
        "PublicIp",
        "ec2:DescribeAddresses",
    ),
    (
        "compute.autoscaling_groups",
        "autoscaling",
        "describe_auto_scaling_groups",
        "AutoScalingGroups",
        "AutoScalingGroupName",
        "autoscaling:DescribeAutoScalingGroups",
    ),
    (
        "compute.scaling_policies",
        "autoscaling",
        "describe_policies",
        "ScalingPolicies",
        "PolicyName",
        "autoscaling:DescribePolicies",
    ),
)

COMPUTE_SNAPSHOT_FILES = (
    "compute.instances",
    "compute.instance_attributes",
    *(entry[0] for entry in _LISTS),
    "compute.images",
    "compute.key_pairs",
    "compute.instance_types",
    "compute.launch_templates",
)

# The request names the attribute in camelCase; the response keys it in PascalCase.
# The snapshot keeps the request spelling, which is what spec 4 §4.1 documents.
_INSTANCE_ATTRIBUTES = (
    ("userData", "UserData"),
    ("disableApiTermination", "DisableApiTermination"),
)
_INVALID_IDS = re.compile(r"\[([^\]]*)\]")


def collect(connection: AwsConnection, ctx: CollectContext) -> dict[str, Any]:
    ec2 = connection.client("ec2")
    autoscaling = connection.client("autoscaling")
    clients = {"ec2": ec2, "autoscaling": autoscaling}

    files: dict[str, Any] = {}

    instances = _collect_instances(ec2, ctx)
    files["compute.instances"] = {"Instances": instances}

    for name, client_name, method, key, id_field, action in _LISTS:
        items = _paginate(clients[client_name], method, key, action, ctx)
        files[name] = {key: sorted(items or [], key=lambda item: item.get(id_field) or "")}

    files["compute.instance_attributes"] = _collect_instance_attributes(ec2, instances, ctx)
    files["compute.images"] = {"Images": _collect_images(ec2, instances, ctx)}
    files["compute.key_pairs"] = {"KeyPairs": _collect_key_pairs(ec2, ctx)}
    files["compute.instance_types"] = {
        "InstanceTypes": _collect_instance_types(ec2, instances, ctx)
    }
    files["compute.launch_templates"] = _collect_launch_templates(ec2, ctx)

    return files


def _collect_instances(ec2: Any, ctx: CollectContext) -> list[dict[str, Any]]:
    """Flattened: the file is a list of instances, not of reservations (spec 4 §4.1)."""
    reservations = _paginate(
        ec2, "describe_instances", "Reservations", "ec2:DescribeInstances", ctx
    )
    if reservations is None:
        return []
    instances = [
        instance for reservation in reservations for instance in reservation.get("Instances", [])
    ]
    return sorted(instances, key=lambda item: item["InstanceId"])


def _collect_instance_attributes(
    ec2: Any, instances: list[dict[str, Any]], ctx: CollectContext
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for instance in instances:
        instance_id = instance["InstanceId"]
        attributes: dict[str, Any] = {}
        for attribute, response_key in _INSTANCE_ATTRIBUTES:
            try:
                response = ec2.describe_instance_attribute(
                    InstanceId=instance_id, Attribute=attribute
                )
            except Exception as exc:
                if is_access_denied(exc):
                    ctx.access_denied("DescribeInstanceAttribute", "ec2:DescribeInstanceAttribute")
                    return result
                raise
            attributes[attribute] = _attribute_value(response, response_key)
        result[instance_id] = attributes
    return result


def _attribute_value(response: dict[str, Any], response_key: str) -> Any:
    value = response.get(response_key)
    if isinstance(value, dict):
        return value.get("Value")
    return value


def _image_ids(instances: list[dict[str, Any]]) -> list[str]:
    return sorted({instance["ImageId"] for instance in instances if instance.get("ImageId")})


def _collect_images(
    ec2: Any, instances: list[dict[str, Any]], ctx: CollectContext
) -> list[dict[str, Any]]:
    remaining = _image_ids(instances)
    while remaining:
        try:
            images = ec2.describe_images(ImageIds=remaining).get("Images", [])
        except Exception as exc:
            if is_access_denied(exc):
                ctx.access_denied("DescribeImages", "ec2:DescribeImages")
                return []
            dropped = _unknown_ids(exc, "InvalidAMIID.NotFound")
            if dropped:
                remaining = [item for item in remaining if item not in dropped]
                continue
            raise
        return sorted(images, key=lambda item: item["ImageId"])
    return []


def _collect_key_pairs(ec2: Any, ctx: CollectContext) -> list[dict[str, Any]]:
    try:
        pairs = ec2.describe_key_pairs(IncludePublicKey=True).get("KeyPairs", [])
    except Exception as exc:
        if is_access_denied(exc):
            ctx.access_denied("DescribeKeyPairs", "ec2:DescribeKeyPairs")
            return []
        raise
    return sorted(pairs, key=lambda item: item.get("KeyName") or "")


def _instance_types(instances: list[dict[str, Any]]) -> list[str]:
    return sorted({item["InstanceType"] for item in instances if item.get("InstanceType")})


def _collect_instance_types(
    ec2: Any, instances: list[dict[str, Any]], ctx: CollectContext
) -> list[dict[str, Any]]:
    remaining = _instance_types(instances)
    while remaining:
        try:
            items: list[dict[str, Any]] = []
            for page in ec2.get_paginator("describe_instance_types").paginate(
                InstanceTypes=remaining
            ):
                items.extend(page.get("InstanceTypes", []))
        except Exception as exc:
            if is_access_denied(exc):
                ctx.access_denied("DescribeInstanceTypes", "ec2:DescribeInstanceTypes")
                return []
            dropped = _unknown_ids(exc, "InvalidInstanceType")
            if dropped:
                remaining = [item for item in remaining if item not in dropped]
                continue
            raise
        return sorted(items, key=lambda item: item["InstanceType"])
    return []


def _collect_launch_templates(ec2: Any, ctx: CollectContext) -> dict[str, Any]:
    templates = _paginate(
        ec2,
        "describe_launch_templates",
        "LaunchTemplates",
        "ec2:DescribeLaunchTemplates",
        ctx,
    )
    if not templates:
        return {}
    result: dict[str, Any] = {}
    for template in sorted(templates, key=lambda item: item["LaunchTemplateId"]):
        template_id = template["LaunchTemplateId"]
        versions = _paginate(
            ec2,
            "describe_launch_template_versions",
            "LaunchTemplateVersions",
            "ec2:DescribeLaunchTemplateVersions",
            ctx,
            LaunchTemplateId=template_id,
        )
        result[template_id] = {
            "template": template,
            "versions": sorted(
                versions or [], key=lambda item: int(item.get("VersionNumber") or 0)
            ),
        }
    return result


def _unknown_ids(exc: BaseException, code: str) -> set[str]:
    """Ids AWS rejected as unknown, parsed out of the error message."""
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return set()
    error = response.get("Error", {})
    if error.get("Code") != code:
        return set()
    match = _INVALID_IDS.search(error.get("Message", ""))
    if not match:
        return set()
    return {item.strip() for item in match.group(1).split(",") if item.strip()}


def _paginate(
    client: Any, method: str, key: str, action: str, ctx: CollectContext, **kwargs: Any
) -> list[dict[str, Any]] | None:
    items: list[dict[str, Any]] = []
    try:
        if client.can_paginate(method):
            for page in client.get_paginator(method).paginate(**kwargs):
                items.extend(page.get(key, []))
        else:
            # Not every EC2 operation has a paginator (describe_addresses, for one).
            items.extend(getattr(client, method)(**kwargs).get(key, []))
    except Exception as exc:
        if is_access_denied(exc):
            ctx.access_denied(action.split(":", 1)[-1], action)
            return None
        raise
    return items
