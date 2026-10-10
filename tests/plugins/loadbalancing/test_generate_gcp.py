"""Targeted load-balancing generation guards (spec 7 §6); the goldens cover whole shapes."""

import pytest

from easymigrate.core.plugin import GenerationError
from easymigrate.plugins.loadbalancing.generate_gcp import generate
from tests.plugins.loadbalancing.test_generate_gcp_golden import resolved_case


def _with_lb_hints(document, **hints):
    objects = [
        o.model_copy(update={"target_hints": {**o.target_hints, **hints}})
        if o.kind == "load_balancer"
        else o
        for o in document.objects
    ]
    return document.model_copy(update={"objects": objects})


def test_missing_shape_is_a_generation_error():
    document, ctx = resolved_case("alb_https_asg")
    objects = [
        o.model_copy(
            update={"target_hints": {k: v for k, v in o.target_hints.items() if k != "shape"}}
        )
        if o.kind == "load_balancer"
        else o
        for o in document.objects
    ]
    with pytest.raises(GenerationError, match="shape"):
        generate(document.model_copy(update={"objects": objects}), ctx)


def test_allowlist_renders_a_cloud_armor_policy_with_a_default_deny():
    document, ctx = resolved_case("alb_https_asg")
    out = generate(_with_lb_hints(document, allowlist=["203.0.113.0/24"]), ctx).render()
    assert 'resource "google_compute_security_policy"' in out
    assert 'src_ip_ranges = ["203.0.113.0/24"]' in out
    assert 'action   = "deny(403)"' in out
    assert "security_policy" in out


def test_skipped_listener_is_not_created():
    document, ctx = resolved_case("alb_https_asg")
    out = generate(_with_lb_hints(document, skipped_listeners=[443]), ctx).render()
    assert "google_compute_target_https_proxy" not in out
    assert 'port_range            = "80"' in out


# ---- review fixes 2026-10-08 -------------------------------------------------------

from easymigrate.ir.loadbalancing import (  # noqa: E402
    Action,
    ListenerRule,
    Redirect,
    WeightedTarget,
)


def _with_rules(document, port, rules, default=None):
    objects = []
    for o in document.objects:
        if o.kind == "load_balancer":
            listeners = []
            for listener in o.listeners:
                if listener.port == port:
                    listener = listener.model_copy(
                        update={"rules": rules, **({"default_action": default} if default else {})}
                    )
                listeners.append(listener)
            o = o.model_copy(update={"listeners": listeners})
        objects.append(o)
    return document.model_copy(update={"objects": objects})


def _fwd(*targets):
    return Action(
        type="forward", target_groups=[WeightedTarget(target_group=t, weight=w) for t, w in targets]
    )


WEB, API = "target_group:web-tg", "target_group:api-tg"


def test_rules_keep_aws_priority_order_not_longest_prefix():
    document, ctx = resolved_case("alb_https_asg")
    rules = [
        ListenerRule(priority=1, paths=["/api/*"], action=_fwd((API, 1))),
        ListenerRule(priority=2, paths=["/api/v2/*"], action=_fwd((WEB, 1))),
    ]
    out = generate(_with_rules(document, 443, rules), ctx).render()
    block = out.split('resource "google_compute_url_map" "web_443"')[1].split("\nresource")[0]
    assert block.index("priority = 1") < block.index("priority = 2")
    assert 'prefix_match = "/api/"' in block and 'prefix_match = "/api/v2/"' in block


def test_overlapping_hosts_each_get_every_rule_that_applies():
    document, ctx = resolved_case("alb_https_asg")
    rules = [
        ListenerRule(priority=1, hosts=["a.com", "b.com"], action=_fwd((API, 1))),
        ListenerRule(priority=2, hosts=["a.com"], paths=["/x"], action=_fwd((WEB, 1))),
    ]
    out = generate(_with_rules(document, 443, rules), ctx).render()
    host_lines = [line.strip() for line in out.splitlines() if line.strip().startswith("hosts")]
    flat = [
        h
        for line in host_lines
        for h in line.split("[")[1].rstrip("]").replace('"', "").split(", ")
    ]
    assert sorted(flat) == ["a.com", "b.com"]  # each host in exactly one host_rule


def test_weighted_forward_becomes_weighted_backend_services():
    document, ctx = resolved_case("alb_https_asg")
    rules = [ListenerRule(priority=5, paths=["/blue*"], action=_fwd((WEB, 90), (API, 10)))]
    out = generate(_with_rules(document, 443, rules), ctx).render()
    assert "weighted_backend_services" in out and "weight          = 90" in out


def test_default_redirect_keeps_the_listeners_rules():
    document, ctx = resolved_case("alb_https_asg")
    redirect = Action(type="redirect", redirect=Redirect(protocol="HTTPS", port="443"))
    rules = [ListenerRule(priority=3, paths=["/health"], action=_fwd((WEB, 1)))]
    out = generate(_with_rules(document, 80, rules, default=redirect), ctx).render()
    block = out.split('resource "google_compute_url_map" "web_80"')[1].split("\nresource")[0]
    assert "default_url_redirect" in block and 'full_path_match = "/health"' in block


def test_redirect_path_template_is_reported_not_copied():
    document, ctx = resolved_case("alb_https_asg")
    redirect = Action(
        type="redirect", redirect=Redirect(protocol="HTTPS", path="/new/#{path}", port="8443")
    )
    out = generate(_with_rules(document, 80, [], default=redirect), ctx).render()
    assert "#{path}" not in out
    assert {"GEN-LB-REDIRECT-PATH", "GEN-LB-REDIRECT-PORT"} <= {f.code for f in ctx.findings}
