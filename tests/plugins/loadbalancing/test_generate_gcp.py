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
