"""Collect raw EKS responses for one region (spec 5 §4.1). No interpretation.

The plugin makes its own Auto Scaling, launch-template and instance-type calls instead of reading
the compute plugin's snapshot, so each plugin stays independently runnable (spec 4 §4.1).
"""

from __future__ import annotations

from typing import Any

from easymigrate.core.plugin import CollectContext
from easymigrate.plugins.compute.normalize import group_template_spec
from easymigrate.providers.aws.source import AwsConnection, is_access_denied

SELF_MANAGED_TAG_PREFIX = "kubernetes.io/cluster/"

KUBERNETES_SNAPSHOT_FILES = (
    "kubernetes.clusters",
    "kubernetes.nodegroups",
    "kubernetes.fargate_profiles",
    "kubernetes.addons",
    "kubernetes.access_entries",
    "kubernetes.pod_identities",
    "kubernetes.self_managed_groups",
    "kubernetes.launch_templates",
    "kubernetes.instance_types",
)


class _Denied(Exception):
    pass


def _call(ctx: CollectContext, action: str, fn, **kwargs: Any) -> dict[str, Any]:
    try:
        return fn(**kwargs)
    except Exception as exc:
        if is_access_denied(exc):
            ctx.access_denied(action.split(":", 1)[-1], action)
            raise _Denied from exc
        raise


def _paginate(client: Any, method: str, key: str, action: str, ctx: CollectContext, **kwargs: Any):
    items: list[Any] = []
    try:
        if client.can_paginate(method):
            for page in client.get_paginator(method).paginate(**kwargs):
                items.extend(page.get(key, []))
        else:
            items.extend(getattr(client, method)(**kwargs).get(key, []))
    except Exception as exc:
        if is_access_denied(exc):
            ctx.access_denied(action.split(":", 1)[-1], action)
            raise _Denied from exc
        raise
    return items


def collect(connection: AwsConnection, ctx: CollectContext) -> dict[str, Any]:
    eks = connection.client("eks")
    files: dict[str, Any] = {name: {} for name in KUBERNETES_SNAPSHOT_FILES}

    try:
        names = sorted(_paginate(eks, "list_clusters", "clusters", "eks:ListClusters", ctx))
    except _Denied:
        names = []

    for name in names:
        try:
            cluster = _call(ctx, "eks:DescribeCluster", eks.describe_cluster, name=name)["cluster"]
        except _Denied:
            continue
        files["kubernetes.clusters"][name] = cluster
        files["kubernetes.nodegroups"][name] = _describe_each(
            eks,
            ctx,
            name,
            "list_nodegroups",
            "nodegroups",
            "eks:ListNodegroups",
            "describe_nodegroup",
            "nodegroupName",
            "nodegroup",
            "eks:DescribeNodegroup",
        )
        files["kubernetes.fargate_profiles"][name] = _describe_each(
            eks,
            ctx,
            name,
            "list_fargate_profiles",
            "fargateProfileNames",
            "eks:ListFargateProfiles",
            "describe_fargate_profile",
            "fargateProfileName",
            "fargateProfile",
            "eks:DescribeFargateProfile",
        )
        files["kubernetes.addons"][name] = _describe_each(
            eks,
            ctx,
            name,
            "list_addons",
            "addons",
            "eks:ListAddons",
            "describe_addon",
            "addonName",
            "addon",
            "eks:DescribeAddon",
        )
        # A CONFIG_MAP-mode cluster keeps access in the aws-auth ConfigMap, which only the
        # Kubernetes API can read; EKS rejects ListAccessEntries for it (live run, 2026-09-22).
        mode = (cluster.get("accessConfig") or {}).get("authenticationMode", "CONFIG_MAP")
        files["kubernetes.access_entries"][name] = (
            []
            if mode == "CONFIG_MAP"
            else _list(
                eks, ctx, "list_access_entries", "accessEntries", "eks:ListAccessEntries", name
            )
        )
        files["kubernetes.pod_identities"][name] = _pod_identities(eks, ctx, name)

    groups = _self_managed_groups(connection.client("autoscaling"), ctx)
    files["kubernetes.self_managed_groups"] = {"AutoScalingGroups": groups}

    ec2 = connection.client("ec2")
    templates = _launch_templates(ec2, ctx, groups, files["kubernetes.nodegroups"])
    files["kubernetes.launch_templates"] = templates
    files["kubernetes.instance_types"] = {
        "InstanceTypes": _instance_types(ec2, ctx, files["kubernetes.nodegroups"], templates)
    }
    return files


def _list(eks: Any, ctx: CollectContext, method: str, key: str, action: str, cluster: str):
    try:
        return sorted(_paginate(eks, method, key, action, ctx, clusterName=cluster), key=str)
    except _Denied:
        return []
    except Exception as exc:
        # Older endpoints (and moto) may not offer access entries or pod identity at all; a
        # missing optional API is an inventory gap, never a failed scan.
        if _unsupported(exc):
            return []
        raise


def _pod_identities(eks: Any, ctx: CollectContext, cluster: str) -> list[dict[str, Any]]:
    """The list call returns summaries without the role; each association is described for it
    (review, 2026-10-08: every pod identity's role was missing)."""
    summaries = _list(
        eks,
        ctx,
        "list_pod_identity_associations",
        "associations",
        "eks:ListPodIdentityAssociations",
        cluster,
    )
    described = []
    for summary in summaries:
        try:
            detail = _call(
                ctx,
                "eks:DescribePodIdentityAssociation",
                eks.describe_pod_identity_association,
                clusterName=cluster,
                associationId=summary["associationId"],
            ).get("association", {})
        except _Denied:
            return [*described, *summaries[len(described) :]]
        except Exception as exc:
            if _unsupported(exc):
                detail = summary
            else:
                raise
        described.append({**summary, **detail})
    return described


def _describe_each(
    eks: Any,
    ctx: CollectContext,
    cluster: str,
    list_method: str,
    list_key: str,
    list_action: str,
    describe_method: str,
    describe_arg: str,
    describe_key: str,
    describe_action: str,
) -> list[dict[str, Any]]:
    try:
        names = sorted(_paginate(eks, list_method, list_key, list_action, ctx, clusterName=cluster))
    except _Denied:
        return []
    except Exception as exc:
        if _unsupported(exc):
            return []
        raise
    described = []
    for item in names:
        try:
            response = _call(
                ctx,
                describe_action,
                getattr(eks, describe_method),
                clusterName=cluster,
                **{describe_arg: item},
            )
        except _Denied:
            return described
        described.append(response[describe_key])
    return described


def _unsupported(exc: BaseException) -> bool:
    if isinstance(exc, NotImplementedError):
        return True
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    # A bare "404" is an endpoint (moto, some partitions) that does not serve the operation;
    # InvalidRequestException is EKS saying the operation does not apply to this cluster.
    return response.get("Error", {}).get("Code") in {
        "UnsupportedOperation",
        "InvalidAction",
        "InvalidRequestException",
        "404",
    }


def _self_managed_groups(autoscaling: Any, ctx: CollectContext) -> list[dict[str, Any]]:
    try:
        groups = _paginate(
            autoscaling,
            "describe_auto_scaling_groups",
            "AutoScalingGroups",
            "autoscaling:DescribeAutoScalingGroups",
            ctx,
        )
    except _Denied:
        return []
    keep = []
    for group in groups:
        keys = {tag.get("Key", "") for tag in group.get("Tags") or []}
        # EKS managed node groups also carry this tag, but they are described by EKS itself.
        if "eks:nodegroup-name" in keys:
            continue
        if any(key.startswith(SELF_MANAGED_TAG_PREFIX) for key in keys):
            keep.append(group)
    return sorted(keep, key=lambda g: g["AutoScalingGroupName"])


def _launch_templates(
    ec2: Any,
    ctx: CollectContext,
    groups: list[dict[str, Any]],
    nodegroups: dict[str, list[dict[str, Any]]],
) -> dict[str, dict[str, Any]]:
    """launch template id -> {version number: LaunchTemplateData} for every referenced version."""
    wanted: set[tuple[str, str]] = set()
    for group in groups:
        spec = group_template_spec(group)
        template = spec.get("LaunchTemplateId") or spec.get("LaunchTemplateName")
        if template:
            wanted.add((template, str(spec.get("Version") or "$Default")))
    for items in nodegroups.values():
        for nodegroup in items:
            spec = nodegroup.get("launchTemplate") or {}
            if spec.get("id"):
                wanted.add((spec["id"], str(spec.get("version") or "$Default")))
    result: dict[str, dict[str, Any]] = {}
    for template_id, version in sorted(wanted):
        by = "LaunchTemplateId" if template_id.startswith("lt-") else "LaunchTemplateName"
        try:
            versions = _call(
                ctx,
                "ec2:DescribeLaunchTemplateVersions",
                ec2.describe_launch_template_versions,
                **{by: template_id},
                Versions=[version],
            ).get("LaunchTemplateVersions", [])
        except _Denied:
            return result
        for entry in versions:
            result.setdefault(template_id, {})[version] = entry.get("LaunchTemplateData") or {}
    return result


def _instance_types(
    ec2: Any,
    ctx: CollectContext,
    nodegroups: dict[str, list[dict[str, Any]]],
    templates: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    names: set[str] = set()
    for items in nodegroups.values():
        for nodegroup in items:
            names.update(nodegroup.get("instanceTypes") or [])
    for versions in templates.values():
        for data in versions.values():
            if data.get("InstanceType"):
                names.add(data["InstanceType"])
    if not names:
        return []
    try:
        items = _paginate(
            ec2,
            "describe_instance_types",
            "InstanceTypes",
            "ec2:DescribeInstanceTypes",
            ctx,
            InstanceTypes=sorted(names),
        )
    except _Denied:
        return []
    return sorted(items, key=lambda item: item["InstanceType"])
