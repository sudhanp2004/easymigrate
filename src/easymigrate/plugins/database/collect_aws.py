"""Collect raw AWS RDS/EC2 API responses for one region (spec §4.1, collector)."""

import re
from typing import Any

from easymigrate.core.plugin import CollectContext
from easymigrate.providers.aws.source import AwsConnection, is_access_denied

# (snapshot name, boto3 method, response list key, id field, IAM action)
_LISTS = (
    (
        "database.db_instances",
        "describe_db_instances",
        "DBInstances",
        "DBInstanceIdentifier",
        "rds:DescribeDBInstances",
    ),
    (
        "database.db_subnet_groups",
        "describe_db_subnet_groups",
        "DBSubnetGroups",
        "DBSubnetGroupName",
        "rds:DescribeDBSubnetGroups",
    ),
    (
        "database.db_parameter_groups",
        "describe_db_parameter_groups",
        "DBParameterGroups",
        "DBParameterGroupName",
        "rds:DescribeDBParameterGroups",
    ),
    (
        "database.db_clusters",
        "describe_db_clusters",
        "DBClusters",
        "DBClusterIdentifier",
        "rds:DescribeDBClusters",
    ),
)

DATABASE_SNAPSHOT_FILES = (
    *(entry[0] for entry in _LISTS),
    "database.db_parameters",
    "database.instance_types",
)

_INVALID_TYPES = re.compile(r"\[([^\]]*)\]")


def collect(connection: AwsConnection, ctx: CollectContext) -> dict[str, Any]:
    rds = connection.client("rds")
    files: dict[str, Any] = {}
    for name, method, key, id_field, action in _LISTS:
        items = _paginate(rds, method, key, action, ctx)
        if items is not None:
            files[name] = {key: sorted(items, key=lambda item: item[id_field])}

    instances = files.get("database.db_instances", {}).get("DBInstances", [])
    parameters = _collect_parameters(rds, instances, ctx)
    if parameters is not None:
        files["database.db_parameters"] = parameters

    ec2 = connection.client("ec2")
    instance_types = _collect_instance_types(ec2, instances, ctx)
    if instance_types is not None:
        files["database.instance_types"] = {
            "InstanceTypes": sorted(instance_types, key=lambda item: item["InstanceType"])
        }

    return files


def _paginate(client: Any, method: str, key: str, action: str, ctx: CollectContext) -> Any | None:
    items: list[dict[str, Any]] = []
    try:
        for page in client.get_paginator(method).paginate():
            items.extend(page.get(key, []))
    except Exception as exc:
        if is_access_denied(exc):
            ctx.access_denied(action.split(":", 1)[-1], action)
            return None
        raise
    return items


def _attached_parameter_groups(instances: list[dict[str, Any]]) -> list[str]:
    names: set[str] = set()
    for instance in instances:
        for group in instance.get("DBParameterGroups") or []:
            name = group.get("DBParameterGroupName")
            if name:
                names.add(name)
    return sorted(names)


def _collect_parameters(
    rds: Any, instances: list[dict[str, Any]], ctx: CollectContext
) -> dict[str, list[dict[str, Any]]] | None:
    result: dict[str, list[dict[str, Any]]] = {}
    for group_name in _attached_parameter_groups(instances):
        items: list[dict[str, Any]] = []
        try:
            for page in rds.get_paginator("describe_db_parameters").paginate(
                DBParameterGroupName=group_name, Source="user"
            ):
                items.extend(page.get("Parameters", []))
        except Exception as exc:
            if is_access_denied(exc):
                ctx.access_denied("DescribeDBParameters", "rds:DescribeDBParameters")
                return None
            raise
        result[group_name] = sorted(items, key=lambda item: item["ParameterName"])
    return result


def _instance_classes(instances: list[dict[str, Any]]) -> list[str]:
    classes = {
        instance["DBInstanceClass"].removeprefix("db.")
        for instance in instances
        if instance.get("DBInstanceClass")
    }
    return sorted(classes)


def _parse_invalid_types(message: str) -> set[str]:
    match = _INVALID_TYPES.search(message)
    if not match:
        return set()
    return {item.strip() for item in match.group(1).split(",") if item.strip()}


def _collect_instance_types(
    ec2: Any, instances: list[dict[str, Any]], ctx: CollectContext
) -> list[dict[str, Any]] | None:
    remaining = _instance_classes(instances)
    while remaining:
        try:
            items: list[dict[str, Any]] = []
            for page in ec2.get_paginator("describe_instance_types").paginate(
                InstanceTypes=remaining
            ):
                items.extend(page.get("InstanceTypes", []))
            return items
        except Exception as exc:
            if is_access_denied(exc):
                ctx.access_denied("DescribeInstanceTypes", "ec2:DescribeInstanceTypes")
                return None
            error = getattr(exc, "response", {}).get("Error", {})
            invalid = (
                _parse_invalid_types(error.get("Message", ""))
                if error.get("Code") == "InvalidInstanceType"
                else set()
            )
            if not invalid:
                raise
            remaining = [c for c in remaining if c not in invalid]
    return []
