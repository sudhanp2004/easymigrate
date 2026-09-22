"""Load-balancing normalization (spec 7 §4)."""

from __future__ import annotations

from typing import Any

from easymigrate.core.plugin import NormalizeContext, Snapshot
from easymigrate.ir.base import SourceRef
from easymigrate.ir.compute import ComputeInstance, InstanceGroup
from easymigrate.ir.loadbalancing import LoadBalancer, TargetGroup
from easymigrate.plugins.loadbalancing.normalize import normalize
from tests.plugins.kubernetes.builders import network_objects

TG = "arn:aws:elasticloadbalancing:ap-south-1:1:targetgroup/web-tg/1"
TG_ASG = "arn:aws:elasticloadbalancing:ap-south-1:1:targetgroup/asg-tg/2"
LB = "arn:aws:elasticloadbalancing:ap-south-1:1:loadbalancer/app/web/3"
CERT = "arn:aws:acm:ap-south-1:1:certificate/c"


def _compute():
    def src(t, i):
        return SourceRef(cloud="aws", type=t, id=i, region="ap-south-1")

    instance = ComputeInstance(
        id="compute_instance:web",
        name="web",
        source=src("instance", "i-web"),
        instance_type="t3.micro",
        architecture="x86_64",
        state="running",
        zone="ap-south-1a",
        subnet="subnet:private-1",
        image="ami-1",
        platform="linux",
    )
    group = InstanceGroup(
        id="instance_group:web-asg",
        name="web-asg",
        source=src("autoScalingGroup", "web-asg"),
        template="instance_template:lt",
        min_size=1,
        max_size=2,
        desired_capacity=1,
    )
    return [instance, group]


def _group(arn, name, targets, **kw):
    group = {
        "TargetGroupArn": arn,
        "TargetGroupName": name,
        "Protocol": "HTTP",
        "Port": 80,
        "VpcId": "vpc-1",
        "TargetType": "instance",
        "HealthCheckProtocol": "HTTP",
        "HealthCheckPath": "/health",
        "HealthCheckPort": "traffic-port",
        "Matcher": {"HttpCode": "200-299"},
    }
    group.update(kw)
    return {
        "TargetGroup": group,
        "Tags": [],
        "Attributes": [
            {"Key": "stickiness.enabled", "Value": "true"},
            {"Key": "stickiness.type", "Value": "lb_cookie"},
            {"Key": "deregistration_delay.timeout_seconds", "Value": "30"},
        ],
        "Targets": [{"Target": {"Id": t, "Port": 80}} for t in targets],
    }


def _files(**over: Any) -> dict[str, Any]:
    files = {
        "loadbalancing.load_balancers": [
            {
                "LoadBalancer": {
                    "LoadBalancerArn": LB,
                    "LoadBalancerName": "web",
                    "Type": "application",
                    "Scheme": "internet-facing",
                    "AvailabilityZones": [{"SubnetId": "subnet-1"}, {"SubnetId": "subnet-2"}],
                    "SecurityGroups": ["sg-nodes"],
                },
                "Tags": [],
                "Attributes": [
                    {"Key": "idle_timeout.timeout_seconds", "Value": "60"},
                    {"Key": "access_logs.s3.enabled", "Value": "true"},
                ],
                "Listeners": [
                    {
                        "Listener": {
                            "Port": 443,
                            "Protocol": "HTTPS",
                            "Certificates": [{"CertificateArn": CERT}],
                            "SslPolicy": "ELBSecurityPolicy-TLS13-1-2-2021-06",
                            "DefaultActions": [{"Type": "forward", "TargetGroupArn": TG}],
                        },
                        "Rules": [
                            {"Priority": "default", "IsDefault": True, "Actions": []},
                            {
                                "Priority": "10",
                                "Conditions": [
                                    {"Field": "path-pattern", "Values": ["/app/*"]},
                                    {"Field": "http-header", "HttpHeaderConfig": {}},
                                ],
                                "Actions": [{"Type": "forward", "TargetGroupArn": TG_ASG}],
                            },
                        ],
                    },
                    {
                        "Listener": {
                            "Port": 80,
                            "Protocol": "HTTP",
                            "DefaultActions": [
                                {
                                    "Type": "redirect",
                                    "RedirectConfig": {
                                        "Protocol": "HTTPS",
                                        "Port": "443",
                                        "StatusCode": "HTTP_301",
                                    },
                                }
                            ],
                        },
                        "Rules": [],
                    },
                ],
            }
        ],
        "loadbalancing.target_groups": [
            _group(TG, "web-tg", ["i-web", "i-gone"]),
            _group(TG_ASG, "asg-tg", ["i-asg-1"]),
        ],
        "loadbalancing.certificates": {
            CERT: {"DomainName": "example.com", "SubjectAlternativeNames": ["www.example.com"]}
        },
        "loadbalancing.target_instances": [
            {"InstanceId": "i-web", "Tags": []},
            {
                "InstanceId": "i-asg-1",
                "Tags": [{"Key": "aws:autoscaling:groupName", "Value": "web-asg"}],
            },
        ],
        "loadbalancing.classic": ["legacy"],
    }
    files.update(over)
    return files


def run(files):
    ctx = NormalizeContext(account_id="1", region="ap-south-1", strict=False)
    network = network_objects()
    ctx.objects = {o.id: o for o in [*network, *_compute()]}
    # The builders' network uses these native ids; add the VPC mapping the target groups use.
    return normalize(Snapshot(files), ctx), ctx


def only(objects, cls):
    return [o for o in objects if isinstance(o, cls)]


def test_targets_resolve_to_instances_and_asg_groups():
    objects, _ = run(_files())
    web, asg = sorted(only(objects, TargetGroup), key=lambda g: g.name, reverse=True)
    assert web.instances == ["compute_instance:web"]
    assert web.unresolved_targets == ["i-gone"]
    assert asg.groups == ["instance_group:web-asg"]
    assert web.stickiness.type == "lb_cookie" and web.deregistration_delay == 30
    assert web.health_check.matcher == "200-299"


def test_listeners_rules_actions_and_certificates():
    lb = only(run(_files())[0], LoadBalancer)[0]
    https, http = lb.listeners
    assert https.certificates[0].domains == ["example.com", "www.example.com"]
    assert https.default_action.target_groups[0].target_group == "target_group:web-tg"
    rule = https.rules[0]
    assert (rule.priority, rule.paths, rule.unsupported_conditions) == (
        10,
        ["/app/*"],
        ["http-header"],
    )
    assert http.default_action.type == "redirect"
    assert http.default_action.redirect.protocol == "HTTPS"
    assert (lb.idle_timeout, lb.access_logs) == (60, True)
    assert lb.subnets == ["subnet:private-1", "subnet:private-2"]
    assert lb.firewall_policies == ["firewall_policy:nodes-sg"]


def test_classic_load_balancers_are_unsupported():
    _, ctx = run(_files())
    assert "LB-CLASSIC-UNSUPPORTED" in [f.code for f in ctx.findings]


def test_kubernetes_controller_load_balancers_are_marked():
    files = _files()
    files["loadbalancing.load_balancers"][0]["Tags"] = [
        {"Key": "elbv2.k8s.aws/cluster", "Value": "prod"}
    ]
    assert only(run(files)[0], LoadBalancer)[0].kubernetes_owned is True
