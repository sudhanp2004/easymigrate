"""registry.gcp_target()/ensure_tofu()/new_tofu() are the seams core.pipeline uses to
reach GCP and OpenTofu; core never imports `google` or `engine.tofu` directly, and
tests fake these by monkeypatching `registry.gcp_target` and
`easymigrate.engine.tofu.ensure_tofu` (plan 4 B2/B6)."""

from __future__ import annotations

from pathlib import Path

import easymigrate.engine.tofu as tofu_module
from easymigrate.core import registry


def test_gcp_target_credentials_ok_delegates_to_state_bucket(monkeypatch):
    monkeypatch.setattr("easymigrate.providers.gcp.state_bucket.credentials_ok", lambda: True)
    assert registry.gcp_target().credentials_ok() is True


def test_gcp_target_ensure_bucket_delegates(monkeypatch):
    calls = []

    def fake_ensure_bucket(gcp_project, name, region, confirm):
        calls.append((gcp_project, name, region))
        return True

    monkeypatch.setattr("easymigrate.providers.gcp.state_bucket.ensure_bucket", fake_ensure_bucket)
    result = registry.gcp_target().ensure_bucket(
        "p", "p-easymigrate-tfstate", "us-east4", lambda n: True
    )

    assert result is True
    assert calls == [("p", "p-easymigrate-tfstate", "us-east4")]


def test_ensure_tofu_uses_engine_tofu_module_attribute_so_it_can_be_faked(monkeypatch, tmp_path):
    fake_binary = tmp_path / "tofu"

    def fake_ensure_tofu(cache_dir=None):
        return fake_binary

    monkeypatch.setattr(tofu_module, "ensure_tofu", fake_ensure_tofu)

    assert registry.ensure_tofu() == fake_binary


def test_new_tofu_returns_a_real_tofu_instance(tmp_path):
    tofu = registry.new_tofu(Path("/bin/tofu"), tmp_path)
    assert isinstance(tofu, tofu_module.Tofu)
    assert tofu.workdir == tmp_path


def test_gcp_target_services_enabled_returns_missing(monkeypatch):
    monkeypatch.setattr(
        "easymigrate.providers.gcp.services.enabled_services",
        lambda project: {"compute.googleapis.com"},
    )

    missing = registry.gcp_target().services_enabled(
        "p", {"compute.googleapis.com", "sqladmin.googleapis.com"}
    )

    assert missing == {"sqladmin.googleapis.com"}


def test_gcp_target_services_enabled_returns_none_when_check_fails(monkeypatch):
    from easymigrate.providers.gcp import services as gcp_services

    def _boom(project):
        raise gcp_services.ServiceUsageCheckFailed("no permission")

    monkeypatch.setattr("easymigrate.providers.gcp.services.enabled_services", _boom)

    assert registry.gcp_target().services_enabled("p", {"sqladmin.googleapis.com"}) is None


def test_database_password_variables_delegates_to_generator(monkeypatch):
    sentinel = {"database_instance:a": "db_password_a"}
    monkeypatch.setattr(
        "easymigrate.core.registry._database_password_variables", lambda resolved: sentinel
    )

    assert registry.database_password_variables(object()) is sentinel
