"""Collect raw AWS EC2 and Auto Scaling responses for one region (spec 4 §4.1, collector)."""

import re
from typing import Any

from easymigrate.core.plugin import CollectContext
from easymigrate.ir.base import Finding
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


# Collected only so `normalize` can report them as unsupported (spec 4 §4.4). A user with these
# would otherwise assume the instances they govern migrated cleanly. Collected tolerantly: an
# inventory call that a partition, region or endpoint does not offer must never abort a scan.
_INVENTORY_LISTS = (
    ("compute.hosts", "describe_hosts", "Hosts", "HostId", "ec2:DescribeHosts"),
    (
        "compute.spot_fleet_requests",
        "describe_spot_fleet_requests",
        "SpotFleetRequestConfigs",
        "SpotFleetRequestId",
        "ec2:DescribeSpotFleetRequests",
    ),
    ("compute.fleets", "describe_fleets", "Fleets", "FleetId", "ec2:DescribeFleets"),
    (
        "compute.capacity_reservations",
        "describe_capacity_reservations",
        "CapacityReservations",
        "CapacityReservationId",
        "ec2:DescribeCapacityReservations",
    ),
)

COMPUTE_SNAPSHOT_FILES = (
    "compute.instances",
    "compute.instance_attributes",
    *(entry[0] for entry in _LISTS),
    *(entry[0] for entry in _INVENTORY_LISTS),
    "compute.images",
    "compute.key_pairs",
    "compute.instance_types",
    "compute.launch_templates",
    "compute.lifecycle_hooks",
    "compute.availability_zones",
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
        files[name] = {key: sorted(items or [], key=lambda item, f=id_field: item.get(f) or "")}

    for name, method, key, id_field, action in _INVENTORY_LISTS:
        items = _inventory(ec2, method, key, action, ctx)
        files[name] = {key: sorted(items, key=lambda item, f=id_field: item.get(f) or "")}

    files["compute.instance_attributes"] = _collect_instance_attributes(ec2, instances, ctx)
    # Templates first: their AMIs and instance types need describing as much as the instances'
    # do, or a template has no platform or sizing and no group built from it can migrate.
    templates = _collect_launch_templates(ec2, ctx)
    files["compute.launch_templates"] = templates
    shapes = [*instances, *_template_data(templates)]
    files["compute.images"] = {"Images": _collect_images(ec2, shapes, ctx)}
    files["compute.key_pairs"] = {"KeyPairs": _collect_key_pairs(ec2, ctx)}
    files["compute.instance_types"] = {"InstanceTypes": _collect_instance_types(ec2, shapes, ctx)}
    files["compute.availability_zones"] = {"AvailabilityZones": _collect_zones(ec2, ctx)}
    files["compute.lifecycle_hooks"] = _collect_lifecycle_hooks(
        autoscaling, files["compute.autoscaling_groups"]["AutoScalingGroups"], ctx
    )

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


def _collect_zones(ec2: Any, ctx: CollectContext) -> list[dict[str, Any]]:
    """Zone names are per-account aliases; the ids are what zone mapping sorts by, and an ASG
    names only its zones, never their ids."""
    try:
        zones = ec2.describe_availability_zones().get("AvailabilityZones", [])
    except Exception as exc:
        if is_access_denied(exc):
            ctx.access_denied("DescribeAvailabilityZones", "ec2:DescribeAvailabilityZones")
            return []
        raise
    return sorted(zones, key=lambda zone: zone.get("ZoneName") or "")


def _template_data(templates: dict[str, Any]) -> list[dict[str, Any]]:
    """Every launch-template version's data, which names an AMI and type like an instance."""
    return [
        version.get("LaunchTemplateData") or {}
        for entry in templates.values()
        for version in entry.get("versions") or []
    ]


def _collect_lifecycle_hooks(
    autoscaling: Any, groups: list[dict[str, Any]], ctx: CollectContext
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for group in groups:
        name = group["AutoScalingGroupName"]
        try:
            hooks = autoscaling.describe_lifecycle_hooks(AutoScalingGroupName=name).get(
                "LifecycleHooks", []
            )
        except Exception as exc:
            if is_access_denied(exc):
                ctx.access_denied("DescribeLifecycleHooks", "autoscaling:DescribeLifecycleHooks")
                return result
            raise
        if hooks:
            result[name] = sorted(hooks, key=lambda hook: hook.get("LifecycleHookName") or "")
    return result


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


def _inventory(
    client: Any, method: str, key: str, action: str, ctx: CollectContext
) -> list[dict[str, Any]]:
    """Best-effort collection for the unsupported-resource inventory.

    `NotImplementedError` and an unsupported-operation error mean the endpoint does not offer
    this call at all. That is a gap in the inventory, not a reason to fail the scan, so it is
    reported as a scan gap and collection continues.
    """
    try:
        items = _paginate(client, method, key, action, ctx)
    except NotImplementedError:
        items = None
    except Exception as exc:
        if not _is_unsupported_operation(exc):
            raise
        items = None
    else:
        return items or []
    ctx.findings.append(
        Finding(
            level="scan_gap",
            code="SCAN-INVENTORY-UNAVAILABLE",
            message=(
                f"{action} is not available at this endpoint, so resources it would list are "
                "missing from the unsupported-resource inventory."
            ),
            subject=action,
        )
    )
    return []


def _is_unsupported_operation(exc: BaseException) -> bool:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    return response.get("Error", {}).get("Code") in {
        "UnsupportedOperation",
        "InvalidAction",
        "InvalidClientTokenId",
    }
