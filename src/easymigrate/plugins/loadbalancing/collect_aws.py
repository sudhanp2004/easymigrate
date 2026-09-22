"""Collect raw Elastic Load Balancing responses for one region (spec 7 §3). No interpretation."""

from __future__ import annotations

from typing import Any

from easymigrate.core.plugin import CollectContext
from easymigrate.providers.aws.source import AwsConnection, is_access_denied

LOADBALANCING_SNAPSHOT_FILES = (
    "loadbalancing.load_balancers",
    "loadbalancing.target_groups",
    "loadbalancing.certificates",
    "loadbalancing.target_instances",
    "loadbalancing.classic",
)

_TAG_BATCH = 20  # DescribeTags accepts at most 20 ARNs per call


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


def _paginate(client: Any, method: str, key: str, action: str, ctx: CollectContext, **kwargs):
    items: list[Any] = []
    try:
        for page in client.get_paginator(method).paginate(**kwargs):
            items.extend(page.get(key, []))
    except Exception as exc:
        if is_access_denied(exc):
            ctx.access_denied(action.split(":", 1)[-1], action)
            raise _Denied from exc
        raise
    return items


def collect(connection: AwsConnection, ctx: CollectContext) -> dict[str, Any]:
    elb = connection.client("elbv2")
    files: dict[str, Any] = {
        "loadbalancing.load_balancers": [],
        "loadbalancing.target_groups": [],
        "loadbalancing.certificates": {},
        "loadbalancing.target_instances": [],
        "loadbalancing.classic": [],
    }
    try:
        lbs = _paginate(
            elb,
            "describe_load_balancers",
            "LoadBalancers",
            "elasticloadbalancing:DescribeLoadBalancers",
            ctx,
        )
    except _Denied:
        lbs = []
    try:
        groups = _paginate(
            elb,
            "describe_target_groups",
            "TargetGroups",
            "elasticloadbalancing:DescribeTargetGroups",
            ctx,
        )
    except _Denied:
        groups = []

    tags = _tags(
        elb, ctx, [lb["LoadBalancerArn"] for lb in lbs] + [g["TargetGroupArn"] for g in groups]
    )
    certificate_arns: set[str] = set()
    for lb in sorted(lbs, key=lambda item: item["LoadBalancerArn"]):
        arn = lb["LoadBalancerArn"]
        entry = {"LoadBalancer": lb, "Tags": tags.get(arn, []), "Attributes": [], "Listeners": []}
        try:
            entry["Attributes"] = _call(
                ctx,
                "elasticloadbalancing:DescribeLoadBalancerAttributes",
                elb.describe_load_balancer_attributes,
                LoadBalancerArn=arn,
            ).get("Attributes", [])
            listeners = _paginate(
                elb,
                "describe_listeners",
                "Listeners",
                "elasticloadbalancing:DescribeListeners",
                ctx,
                LoadBalancerArn=arn,
            )
        except _Denied:
            listeners = []
        for listener in sorted(listeners, key=lambda item: item.get("Port", 0)):
            try:
                rules = _call(
                    ctx,
                    "elasticloadbalancing:DescribeRules",
                    elb.describe_rules,
                    ListenerArn=listener["ListenerArn"],
                ).get("Rules", [])
            except _Denied:
                rules = []
            certificate_arns.update(c["CertificateArn"] for c in listener.get("Certificates") or [])
            entry["Listeners"].append({"Listener": listener, "Rules": rules})
        files["loadbalancing.load_balancers"].append(entry)

    instance_ids: set[str] = set()
    for group in sorted(groups, key=lambda item: item["TargetGroupArn"]):
        arn = group["TargetGroupArn"]
        entry = {"TargetGroup": group, "Tags": tags.get(arn, []), "Attributes": [], "Targets": []}
        try:
            entry["Attributes"] = _call(
                ctx,
                "elasticloadbalancing:DescribeTargetGroupAttributes",
                elb.describe_target_group_attributes,
                TargetGroupArn=arn,
            ).get("Attributes", [])
            entry["Targets"] = _call(
                ctx,
                "elasticloadbalancing:DescribeTargetHealth",
                elb.describe_target_health,
                TargetGroupArn=arn,
            ).get("TargetHealthDescriptions", [])
        except _Denied:
            pass
        if group.get("TargetType", "instance") == "instance":
            instance_ids.update(t["Target"]["Id"] for t in entry["Targets"])
        files["loadbalancing.target_groups"].append(entry)

    files["loadbalancing.certificates"] = _certificates(connection, ctx, sorted(certificate_arns))
    files["loadbalancing.target_instances"] = _instances(connection, ctx, sorted(instance_ids))
    files["loadbalancing.classic"] = _classic(connection, ctx)
    return files


def _tags(elb: Any, ctx: CollectContext, arns: list[str]) -> dict[str, list[dict[str, str]]]:
    result: dict[str, list[dict[str, str]]] = {}
    for start in range(0, len(arns), _TAG_BATCH):
        try:
            descriptions = _call(
                ctx,
                "elasticloadbalancing:DescribeTags",
                elb.describe_tags,
                ResourceArns=arns[start : start + _TAG_BATCH],
            ).get("TagDescriptions", [])
        except _Denied:
            return result
        for item in descriptions:
            result[item["ResourceArn"]] = item.get("Tags", [])
    return result


def _certificates(connection: AwsConnection, ctx: CollectContext, arns: list[str]) -> dict:
    if not arns:
        return {}
    acm = connection.client("acm")
    result: dict[str, Any] = {}
    for arn in arns:
        try:
            certificate = _call(
                ctx, "acm:DescribeCertificate", acm.describe_certificate, CertificateArn=arn
            ).get("Certificate", {})
        except _Denied:
            return result
        except Exception:
            # A certificate from IAM (not ACM) or deleted since: record what the listener said.
            certificate = {}
        result[arn] = {
            "DomainName": certificate.get("DomainName"),
            "SubjectAlternativeNames": certificate.get("SubjectAlternativeNames") or [],
        }
    return result


def _instances(connection: AwsConnection, ctx: CollectContext, ids: list[str]) -> list[dict]:
    if not ids:
        return []
    try:
        reservations = _paginate(
            connection.client("ec2"),
            "describe_instances",
            "Reservations",
            "ec2:DescribeInstances",
            ctx,
            InstanceIds=ids,
        )
    except _Denied:
        return []
    return sorted(
        (
            {"InstanceId": i["InstanceId"], "Tags": i.get("Tags") or []}
            for r in reservations
            for i in r.get("Instances", [])
        ),
        key=lambda i: i["InstanceId"],
    )


def _classic(connection: AwsConnection, ctx: CollectContext) -> list[str]:
    try:
        items = _paginate(
            connection.client("elb"),
            "describe_load_balancers",
            "LoadBalancerDescriptions",
            "elasticloadbalancing:DescribeLoadBalancers",
            ctx,
        )
    except _Denied:
        return []
    return sorted(item["LoadBalancerName"] for item in items)
