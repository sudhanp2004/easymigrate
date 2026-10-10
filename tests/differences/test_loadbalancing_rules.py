"""AWS→GCP load-balancing rules (spec 7 §5)."""

from __future__ import annotations

import pytest

from easymigrate.differences.rules.aws_gcp import AWS_GCP_RULES
from easymigrate.differences.rules.aws_gcp.loadbalancing import (
    LbAccessLogs,
    LbBackends,
    LbCertificates,
    LbHealthCheck,
    LbKubernetesOwned,
    LbNewAddress,
    LbNlbPortTranslation,
    LbRuleConditions,
    LbShape,
    LbSourceRestriction,
    LbSslPolicy,
    LbStickiness,
    LbUnsupportedActions,
)
from easymigrate.ir.base import IRDocument, SourceRef
from easymigrate.ir.loadbalancing import (
    Action,
    Certificate,
    HealthCheck,
    Listener,
    ListenerRule,
    LoadBalancer,
    Stickiness,
    TargetGroup,
    WeightedTarget,
)
from easymigrate.ir.network import CidrPeer, FirewallRule
from tests.plugins.kubernetes.builders import network_objects


def _src(t, i):
    return SourceRef(cloud="aws", type=t, id=i, region="ap-south-1")


def _forward(tg="target_group:web"):
    return Action(type="forward", target_groups=[WeightedTarget(target_group=tg)])


def _tg(id_="target_group:web", **kw):
    fields = dict(
        id=id_,
        name=id_.split(":", 1)[1],
        source=_src("target-group", id_),
        protocol="HTTP",
        port=80,
    )
    fields.update(kw)
    return TargetGroup(**fields)


def _lb(id_="load_balancer:web", listeners=None, **kw):
    fields = dict(
        id=id_,
        name=id_.split(":", 1)[1],
        source=_src("load-balancer", id_),
        lb_type="application",
        scheme="internet-facing",
        subnets=["subnet:private-1", "subnet:private-2"],
        listeners=listeners
        if listeners is not None
        else [Listener(port=80, protocol="HTTP", default_action=_forward())],
    )
    fields.update(kw)
    return LoadBalancer(**fields)


def _doc(*objects, network=None):
    return IRDocument(source={"cloud": "aws"}, objects=[*(network or network_objects()), *objects])


def _only(rule, doc):
    diffs = rule.detect(doc)
    assert len(diffs) == 1
    return diffs[0]


def _hints(doc, obj_id="load_balancer:web"):
    return doc.get(obj_id).target_hints


@pytest.mark.parametrize(
    ("lb_type", "scheme", "shape"),
    [
        ("application", "internet-facing", "global_external_alb"),
        ("application", "internal", "regional_internal_alb"),
        ("network", "internet-facing", "external_passthrough_nlb"),
        ("network", "internal", "internal_passthrough_nlb"),
    ],
)
def test_shape_maps_each_type(lb_type, scheme, shape):
    rule = LbShape()
    doc = _doc(_tg(), _lb(lb_type=lb_type, scheme=scheme))
    diff = _only(rule, doc)
    hints = _hints(rule.apply(doc, diff, "map"))
    assert hints["shape"] == shape
    assert hints["network"] == "network:vpc" and hints["subnet"] == "subnet:private-1"
    if shape == "regional_internal_alb":
        assert hints["proxy_subnet_cidr"] == "100.126.0.0/23"
        assert rule.risk_for(diff, "map") == "medium"


def test_new_address_is_stated_at_high_risk():
    rule = LbNewAddress()
    assert rule.risk_for(_only(rule, _doc(_tg(), _lb())), "reserve_ip") == "high"


def test_kubernetes_owned_load_balancer_is_excluded():
    rule = LbKubernetesOwned()
    doc = _doc(_tg(), _lb(kubernetes_owned=True))
    assert rule.apply(doc, _only(rule, doc), "exclude").get("load_balancer:web").excluded


def test_backends_name_a_port_and_mark_asg_groups():
    from easymigrate.ir.compute import InstanceGroup, InstanceTemplate

    template = InstanceTemplate(
        id="instance_template:lt",
        name="lt",
        source=_src("lt", "lt"),
        instance_type="t3.micro",
        architecture="x86_64",
        image="ami-1",
        platform="linux",
        version="1",
    )
    group = InstanceGroup(
        id="instance_group:web-asg",
        name="web-asg",
        source=_src("asg", "web-asg"),
        template="instance_template:lt",
        min_size=1,
        max_size=2,
        desired_capacity=1,
    )
    rule = LbBackends()
    doc = _doc(template, group, _tg(groups=["instance_group:web-asg"], port=8080), _lb())
    diff = _only(rule, doc)
    result = rule.apply(doc, diff, "instance_groups")
    named = {"name": "tg-web", "port": 8080}
    assert _hints(result, "target_group:web")["named_port"] == named
    assert _hints(result, "instance_group:web-asg")["named_ports"] == [named]


def test_non_instance_target_groups_are_manual_and_excluded():
    rule = LbBackends()
    doc = _doc(_tg(target_type="ip"), _lb())
    diff = _only(rule, doc)
    assert [s.id for s in rule.allowed_strategies(diff)] == ["manual"]
    assert rule.apply(doc, diff, "manual").get("target_group:web").excluded


def test_health_check_maps_and_flags_non_200_matchers():
    rule = LbHealthCheck()
    doc = _doc(
        _tg(health_check=HealthCheck(path="/health", matcher="200-299", timeout=10, interval=5)),
        _lb(),
    )
    diff = _only(rule, doc)
    assert rule.risk_for(diff, "map_health_check") == "medium"
    check = _hints(rule.apply(doc, diff, "map_health_check"), "target_group:web")["health_check"]
    assert check == {
        "protocol": "HTTP",
        "port": 80,
        "request_path": "/health",
        "check_interval_sec": 5,
        "timeout_sec": 5,  # GCP requires timeout <= interval
        "healthy_threshold": 5,
        "unhealthy_threshold": 2,
    }


def test_stickiness_becomes_cookie_affinity():
    rule = LbStickiness()
    doc = _doc(_tg(stickiness=Stickiness(type="lb_cookie", duration=3600)), _lb())
    affinity = _hints(rule.apply(doc, _only(rule, doc), "cookie_affinity"), "target_group:web")[
        "affinity"
    ]
    assert affinity == {"mode": "GENERATED_COOKIE", "ttl": 3600}


def _https(domains=("example.com",), policy="ELBSecurityPolicy-TLS13-1-2-2021-06"):
    return Listener(
        port=443,
        protocol="HTTPS",
        certificates=[Certificate(arn="arn:c", domains=list(domains))],
        ssl_policy=policy,
        default_action=_forward(),
    )


def test_certificates_become_google_managed_for_the_same_domains():
    rule = LbCertificates()
    doc = _doc(_tg(), _lb(listeners=[_https(("example.com", "www.example.com"))]))
    diff = _only(rule, doc)
    assert diff.key == "LB-CERTIFICATES@load_balancer:web:443"
    assert _hints(rule.apply(doc, diff, "google_managed"))["certificates"] == {
        "443": ["example.com", "www.example.com"]
    }


def test_wildcard_certificates_are_manual_and_skip_the_listener():
    rule = LbCertificates()
    doc = _doc(_tg(), _lb(listeners=[_https(("*.example.com",))]))
    diff = _only(rule, doc)
    assert [s.id for s in rule.allowed_strategies(diff)] == ["manual"]
    assert _hints(rule.apply(doc, diff, "manual"))["skipped_listeners"] == [443]


def test_ssl_policy_keeps_the_minimum_tls_version():
    rule = LbSslPolicy()
    doc = _doc(_tg(), _lb(listeners=[_https()]))
    policies = _hints(rule.apply(doc, _only(rule, doc), "map_policy"))["ssl_policies"]
    assert policies == {"443": {"profile": "MODERN", "min_tls": "TLS_1_2"}}


def _with_ingress(cidrs):
    network = network_objects()
    rules = [
        FirewallRule(direction="ingress", protocol="tcp", ports=["443"], peer=CidrPeer(cidr=c))
        for c in cidrs
    ]
    return [
        o.model_copy(update={"rules": rules}) if o.id == "firewall_policy:nodes-sg" else o
        for o in network
    ]


def test_restricted_alb_gets_a_cloud_armor_allowlist():
    rule = LbSourceRestriction()
    doc = _doc(
        _tg(),
        _lb(firewall_policies=["firewall_policy:nodes-sg"]),
        network=_with_ingress(["203.0.113.0/24"]),
    )
    diff = _only(rule, doc)
    assert _hints(rule.apply(doc, diff, "cloud_armor_allowlist"))["allowlist"] == ["203.0.113.0/24"]


def test_world_open_alb_needs_no_allowlist():
    doc = _doc(
        _tg(),
        _lb(firewall_policies=["firewall_policy:nodes-sg"]),
        network=_with_ingress(["0.0.0.0/0"]),
    )
    assert LbSourceRestriction().detect(doc) == []


def test_rules_with_unsupported_conditions_or_inner_wildcards_are_dropped():
    rule = LbRuleConditions()
    listener = Listener(
        port=80,
        protocol="HTTP",
        default_action=_forward(),
        rules=[
            ListenerRule(priority=1, paths=["/api/*"], action=_forward()),
            ListenerRule(priority=2, paths=["/a/*/b"], action=_forward()),
            ListenerRule(
                priority=3,
                hosts=["x.com"],
                unsupported_conditions=["http-header"],
                action=_forward(),
            ),
        ],
    )
    doc = _doc(_tg(), _lb(listeners=[listener]))
    hints = _hints(rule.apply(doc, _only(rule, doc), "drop_rule"))
    assert hints["dropped_rules"] == [{"port": 80, "priority": 2}, {"port": 80, "priority": 3}]


def test_default_fixed_response_falls_back_to_the_last_forward_rule_at_high_risk():
    rule = LbUnsupportedActions()
    listener = Listener(
        port=80,
        protocol="HTTP",
        default_action=Action(type="fixed-response"),
        rules=[
            ListenerRule(priority=5, paths=["/a*"], action=_forward("target_group:a")),
            ListenerRule(priority=9, paths=["/b*"], action=_forward("target_group:b")),
            ListenerRule(priority=10, paths=["/login"], action=Action(type="authenticate")),
        ],
    )
    doc = _doc(_tg("target_group:a"), _tg("target_group:b"), _lb(listeners=[listener]))
    diff = _only(rule, doc)
    assert rule.risk_for(diff, "drop_rule") == "high"
    hints = _hints(rule.apply(doc, diff, "drop_rule"))
    assert hints["default_fallbacks"] == {"80": "target_group:b"}
    assert hints["dropped_rules"] == [{"port": 80, "priority": 10}]


def test_nlb_port_translation_skips_the_listener():
    rule = LbNlbPortTranslation()
    listener = Listener(port=443, protocol="TCP", default_action=_forward())
    doc = _doc(_tg(protocol="TCP", port=8443), _lb(lb_type="network", listeners=[listener]))
    diff = _only(rule, doc)
    assert _hints(rule.apply(doc, diff, "manual"))["skipped_listeners"] == [443]


def test_access_logs_become_cloud_logging():
    rule = LbAccessLogs()
    doc = _doc(_tg(), _lb(access_logs=True))
    assert _hints(rule.apply(doc, _only(rule, doc), "cloud_logging")) == {"logging": True}


def test_load_balancing_rules_run_after_compute_and_kubernetes_owned_first():
    ids = [rule.id for rule in AWS_GCP_RULES]
    lb = [i for i in ids if i.startswith("LB-")]
    assert lb[0] == "LB-KUBERNETES-OWNED"
    assert ids.index("VM-ASG-SHAPE") < ids.index("LB-BACKENDS")


def test_dropped_authenticate_rule_is_high_risk():
    rule = LbUnsupportedActions()
    listener = Listener(
        port=80,
        protocol="HTTP",
        default_action=_forward(),
        rules=[ListenerRule(priority=1, paths=["/admin*"], action=Action(type="authenticate"))],
    )
    diff = _only(rule, _doc(_tg(), _lb(listeners=[listener])))
    assert rule.risk_for(diff, "drop_rule") == "high"


def test_load_balancer_with_no_scanned_subnet_is_excluded():
    rule = LbShape()
    doc = _doc(_tg(), _lb(subnets=[]))
    assert "subnets" in rule.apply(doc, _only(rule, doc), "map").get("load_balancer:web").excluded
