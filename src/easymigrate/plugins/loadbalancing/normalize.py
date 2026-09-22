"""Load-balancing normalizer: snapshot → `load_balancer`, `target_group` (spec 7 §4).

Faithful capture only. Targets resolve through `ctx.objects`: an instance launched by an Auto
Scaling group becomes a reference to that group's `instance_group`, since compute migrates the group
rather than the instance.
"""

from __future__ import annotations

from typing import Any

from easymigrate.core.plugin import NormalizeContext, Snapshot
from easymigrate.ir.base import Finding, IntentObject, SourceRef
from easymigrate.ir.loadbalancing import (
    Action,
    Certificate,
    HealthCheck,
    Listener,
    ListenerRule,
    LoadBalancer,
    Redirect,
    Stickiness,
    TargetGroup,
    WeightedTarget,
)
from easymigrate.ir.naming import allocate_ids, clean_labels

_ASG_TAG = "aws:autoscaling:groupName"
_K8S_TAGS = ("elbv2.k8s.aws/cluster", "ingress.k8s.aws/stack", "service.k8s.aws/stack")


def _tags(items: list[dict[str, str]]) -> dict[str, str]:
    return {t["Key"]: t.get("Value", "") for t in items or []}


def _lookups(ctx: NormalizeContext) -> dict[str, dict[str, str]]:
    by_kind: dict[str, dict[str, str]] = {}
    for obj in ctx.objects.values():
        by_kind.setdefault(obj.kind, {})[obj.source.id] = obj.id
    return by_kind


def normalize(snapshot: Snapshot, ctx: NormalizeContext) -> list[IntentObject]:
    lbs_raw: list[dict[str, Any]] = snapshot.get("loadbalancing.load_balancers") or []
    groups_raw: list[dict[str, Any]] = snapshot.get("loadbalancing.target_groups") or []
    certificates: dict[str, Any] = snapshot.get("loadbalancing.certificates") or {}
    instances_raw: list[dict[str, Any]] = snapshot.get("loadbalancing.target_instances") or []
    classic: list[str] = snapshot.get("loadbalancing.classic") or []
    lookups = _lookups(ctx)

    for name in classic:
        ctx.findings.append(
            Finding(
                level="unsupported",
                code="LB-CLASSIC-UNSUPPORTED",
                message=f"Classic Load Balancer {name} is not migrated; migrate it to an ALB or "
                "NLB in AWS first, or recreate it by hand.",
                subject=name,
            )
        )

    group_ids = allocate_ids(
        "target_group",
        [
            (g["TargetGroup"]["TargetGroupName"], g["TargetGroup"]["TargetGroupArn"])
            for g in groups_raw
        ],
    )
    by_arn = {
        g["TargetGroup"]["TargetGroupArn"]: group_ids[g["TargetGroup"]["TargetGroupArn"]]
        for g in groups_raw
    }
    asg_of = {i["InstanceId"]: _tags(i.get("Tags")).get(_ASG_TAG) for i in instances_raw}

    objects: list[IntentObject] = []
    for entry in groups_raw:
        objects.append(_target_group(entry, by_arn, asg_of, lookups, ctx))

    lb_ids = allocate_ids(
        "load_balancer",
        [
            (e["LoadBalancer"]["LoadBalancerName"], e["LoadBalancer"]["LoadBalancerArn"])
            for e in lbs_raw
        ],
    )
    for entry in lbs_raw:
        lb = entry["LoadBalancer"]
        lb_type = lb.get("Type", "application")
        if lb_type not in {"application", "network"}:
            ctx.findings.append(
                Finding(
                    level="unsupported",
                    code="LB-TYPE-UNSUPPORTED",
                    message=f"{lb_type} load balancer {lb['LoadBalancerName']} is not migrated.",
                    subject=lb["LoadBalancerName"],
                )
            )
            continue
        objects.append(
            _load_balancer(lb_ids[lb["LoadBalancerArn"]], entry, by_arn, certificates, lookups, ctx)
        )
    return objects


def _attributes(items: list[dict[str, str]]) -> dict[str, str]:
    return {a["Key"]: a.get("Value", "") for a in items or []}


def _target_group(
    entry: dict[str, Any],
    by_arn: dict[str, str],
    asg_of: dict[str, str | None],
    lookups: dict[str, dict[str, str]],
    ctx: NormalizeContext,
) -> TargetGroup:
    group = entry["TargetGroup"]
    attrs = _attributes(entry.get("Attributes"))
    target_type = group.get("TargetType", "instance")
    instances: set[str] = set()
    asg_groups: set[str] = set()
    unresolved: set[str] = set()
    for target in entry.get("Targets") or []:
        native = target["Target"]["Id"]
        if target_type != "instance":
            unresolved.add(native)
            continue
        asg = asg_of.get(native)
        if asg and asg in lookups.get("instance_group", {}):
            asg_groups.add(lookups["instance_group"][asg])
        elif native in lookups.get("compute_instance", {}):
            instances.add(lookups["compute_instance"][native])
        else:
            unresolved.add(native)
    stickiness = None
    if attrs.get("stickiness.enabled") == "true":
        kind = attrs.get("stickiness.type", "lb_cookie")
        stickiness = Stickiness(
            type=kind if kind in {"lb_cookie", "app_cookie", "source_ip"} else "lb_cookie",
            cookie_name=attrs.get("stickiness.app_cookie.cookie_name") or None,
            duration=int(attrs.get("stickiness.lb_cookie.duration_seconds") or 0) or None,
        )
    matcher = (group.get("Matcher") or {}).get("HttpCode")
    return TargetGroup(
        id=by_arn[group["TargetGroupArn"]],
        name=group["TargetGroupName"],
        labels=clean_labels(_tags(entry.get("Tags"))),
        source=SourceRef(
            cloud="aws", type="target-group", id=group["TargetGroupArn"], region=ctx.region
        ),
        protocol=group.get("Protocol", "TCP"),
        port=group.get("Port"),
        target_type=target_type if target_type in {"instance", "ip", "lambda", "alb"} else "ip",
        network=lookups.get("network", {}).get(group.get("VpcId", "")),
        health_check=HealthCheck(
            protocol=group.get("HealthCheckProtocol", "HTTP"),
            port=str(group.get("HealthCheckPort", "traffic-port")),
            path=group.get("HealthCheckPath"),
            interval=group.get("HealthCheckIntervalSeconds", 30),
            timeout=group.get("HealthCheckTimeoutSeconds", 5),
            healthy=group.get("HealthyThresholdCount", 5),
            unhealthy=group.get("UnhealthyThresholdCount", 2),
            matcher=matcher,
        ),
        stickiness=stickiness,
        deregistration_delay=int(attrs["deregistration_delay.timeout_seconds"])
        if attrs.get("deregistration_delay.timeout_seconds")
        else None,
        instances=sorted(instances),
        groups=sorted(asg_groups),
        unresolved_targets=sorted(unresolved),
    )


def _action(actions: list[dict[str, Any]], by_arn: dict[str, str]) -> Action:
    # ELB orders actions; authentication comes before the terminal action.
    ordered = sorted(actions or [], key=lambda a: a.get("Order", 0))
    kinds = [a.get("Type", "") for a in ordered]
    if any(k.startswith("authenticate") for k in kinds):
        return Action(type="authenticate")
    terminal = ordered[-1] if ordered else {"Type": "fixed-response"}
    kind = terminal.get("Type")
    if kind == "forward":
        weighted = (terminal.get("ForwardConfig") or {}).get("TargetGroups") or []
        if not weighted and terminal.get("TargetGroupArn"):
            weighted = [{"TargetGroupArn": terminal["TargetGroupArn"], "Weight": 1}]
        return Action(
            type="forward",
            target_groups=[
                WeightedTarget(target_group=by_arn[w["TargetGroupArn"]], weight=w.get("Weight", 1))
                for w in weighted
                if w["TargetGroupArn"] in by_arn
            ],
        )
    if kind == "redirect":
        config = terminal.get("RedirectConfig") or {}
        return Action(
            type="redirect",
            redirect=Redirect(
                protocol=config.get("Protocol"),
                port=config.get("Port"),
                host=config.get("Host"),
                path=config.get("Path"),
                status=config.get("StatusCode", "HTTP_301"),
            ),
        )
    return Action(type="fixed-response")


def _rule(rule: dict[str, Any], by_arn: dict[str, str]) -> ListenerRule:
    hosts: list[str] = []
    paths: list[str] = []
    unsupported: list[str] = []
    for condition in rule.get("Conditions") or []:
        field = condition.get("Field", "")
        if field == "host-header":
            hosts += (condition.get("HostHeaderConfig") or {}).get("Values") or condition.get(
                "Values", []
            )
        elif field == "path-pattern":
            paths += (condition.get("PathPatternConfig") or {}).get("Values") or condition.get(
                "Values", []
            )
        else:
            unsupported.append(field)
    return ListenerRule(
        priority=int(rule["Priority"]),
        hosts=sorted(hosts),
        paths=sorted(paths),
        unsupported_conditions=sorted(unsupported),
        action=_action(rule.get("Actions") or [], by_arn),
    )


def _load_balancer(
    object_id: str,
    entry: dict[str, Any],
    by_arn: dict[str, str],
    certificates: dict[str, Any],
    lookups: dict[str, dict[str, str]],
    ctx: NormalizeContext,
) -> LoadBalancer:
    lb = entry["LoadBalancer"]
    tags = _tags(entry.get("Tags"))
    attrs = _attributes(entry.get("Attributes"))
    listeners: list[Listener] = []
    for item in entry.get("Listeners") or []:
        listener = item["Listener"]
        rules = [_rule(r, by_arn) for r in item.get("Rules") or [] if not r.get("IsDefault")]
        certs = []
        for cert in listener.get("Certificates") or []:
            info = certificates.get(cert["CertificateArn"]) or {}
            domains = [
                d for d in [info.get("DomainName"), *info.get("SubjectAlternativeNames", [])] if d
            ]
            certs.append(Certificate(arn=cert["CertificateArn"], domains=sorted(set(domains))))
        listeners.append(
            Listener(
                port=listener["Port"],
                protocol=listener.get("Protocol", "TCP"),
                certificates=certs,
                ssl_policy=listener.get("SslPolicy"),
                default_action=_action(listener.get("DefaultActions") or [], by_arn),
                rules=sorted(rules, key=lambda r: r.priority),
            )
        )
    subnets = [
        lookups.get("subnet", {}).get(zone.get("SubnetId", ""))
        for zone in lb.get("AvailabilityZones") or []
    ]
    policies = [lookups.get("firewall_policy", {}).get(sg) for sg in lb.get("SecurityGroups") or []]
    return LoadBalancer(
        id=object_id,
        name=lb["LoadBalancerName"],
        labels=clean_labels(tags),
        source=SourceRef(
            cloud="aws", type="load-balancer", id=lb["LoadBalancerArn"], region=ctx.region
        ),
        lb_type=lb.get("Type", "application"),
        scheme=lb.get("Scheme", "internet-facing"),
        subnets=sorted(s for s in subnets if s),
        firewall_policies=sorted(p for p in policies if p),
        listeners=listeners,
        idle_timeout=int(attrs["idle_timeout.timeout_seconds"])
        if attrs.get("idle_timeout.timeout_seconds")
        else None,
        access_logs=attrs.get("access_logs.s3.enabled") == "true",
        deletion_protection=attrs.get("deletion_protection.enabled") == "true",
        kubernetes_owned=any(key in tags for key in _K8S_TAGS),
    )
