"""destroy() tests: the undo for apply. OpenTofu and GCP are faked through the usual seams."""

from __future__ import annotations

from pathlib import Path

import pytest

from easymigrate import __version__
from easymigrate.core.errors import Declined, EasyMigrateError
from easymigrate.core.pipeline import destroy
from easymigrate.core.project import Project, ProjectConfig, TargetConfig
from easymigrate.engine.tofu import TofuError

_NETWORKING_ERROR = (
    "Error: Unable to remove Service Networking Connection, err: Error waiting for Delete "
    "Service Networking Connection: Error code 9, message: Failed to delete connection; "
    "Producer services (e.g. CloudSQL, Cloud Memstore, etc.) are still using this connection."
)


def _resource(address: str, **values) -> dict:
    kind, name = address.split(".", 1)
    return {"address": address, "mode": "managed", "type": kind, "name": name, "values": values}


class FakeTofu:
    def __init__(self, resources: list[dict], fail_first_apply: str | None = None):
        self.resources = resources
        self.fail_first_apply = fail_first_apply
        self.calls: list[tuple] = []

    def init(self, backend_config=None, *, backend=True):
        self.calls.append(("init", backend_config))

    def state_json(self) -> dict:
        if not self.resources:
            return {}
        return {"values": {"root_module": {"resources": self.resources}}}

    def plan_destroy(self, out: Path, stream) -> str:
        self.calls.append(("plan_destroy",))
        out.write_bytes(b"destroy-plan")
        return ""

    def apply_targets(self, targets, stream):
        self.calls.append(("apply_targets", tuple(targets)))
        for resource in self.resources:
            if resource["address"] in targets:
                resource["values"]["deletion_protection"] = False
                for settings in resource["values"].get("settings") or []:
                    settings["deletion_protection_enabled"] = False

    def apply(self, plan: Path, stream) -> None:
        self.calls.append(("apply", plan.name))
        if self.fail_first_apply:
            message, self.fail_first_apply = self.fail_first_apply, None
            # Everything but the connection and the network it peers with is gone by then.
            self.resources = [
                r
                for r in self.resources
                if r["type"] in {"google_service_networking_connection", "google_compute_network"}
            ]
            raise TofuError(message)
        self.resources = []
        stream("Destroy complete!")


class FakeGcp:
    def __init__(self, creds_ok: bool = True):
        self.creds_ok = creds_ok
        self.peerings: list[tuple] = []
        self.deleted_buckets: list[str] = []

    bucket_exists = True

    def ensure_bucket(self, gcp_project, name, region, confirm):
        assert confirm(name) is False, "destroy must never create the state bucket"
        return self.bucket_exists

    def credentials_ok(self):
        return self.creds_ok

    def remove_peering(self, gcp_project, network, peering):
        self.peerings.append((gcp_project, network, peering))

    def delete_bucket(self, gcp_project, name):
        self.deleted_buckets.append(name)
        return True


def _applied_project(tmp_path, tf: str = 'resource "x" "y" {}\n') -> Project:
    project = Project(tmp_path)
    project.init()
    project.save_config(
        ProjectConfig(
            tool_version=__version__,
            target=TargetConfig(cloud="gcp", project="my-proj", region="asia-south1"),
        )
    )
    project.terraform_dir.mkdir(parents=True, exist_ok=True)
    (project.terraform_dir / "main.tf").write_text(tf)
    project.tfplan_path.write_bytes(b"old-plan")
    project.plan_lock_path.write_text("{}")
    return project


def _run(project, tofu, gcp=None, *, answer=True, **kwargs):
    seen: list = []
    result = destroy(
        project,
        confirm=lambda summary: seen.append(summary) or answer,
        stream=lambda line: None,
        tofu=tofu,
        gcp=gcp or FakeGcp(),
        **kwargs,
    )
    return result, seen


def test_destroys_every_resource_in_state_after_confirmation(tmp_path):
    project = _applied_project(tmp_path)
    tofu = FakeTofu(
        [_resource("google_compute_network.main"), _resource("google_storage_bucket.b")]
    )

    result, seen = _run(project, tofu)

    assert seen[0].addresses == ["google_compute_network.main", "google_storage_bucket.b"]
    assert seen[0].gcp_project == "my-proj"
    assert tofu.calls[0] == (
        "init",
        {"bucket": "my-proj-easymigrate-tfstate", "prefix": "easymigrate"},
    )
    assert ("apply", "destroy.tfplan") in tofu.calls
    assert tofu.resources == []
    assert result.peerings_removed == []
    # The old plan must never be replayed onto the empty project.
    assert not project.tfplan_path.exists()
    assert not project.plan_lock_path.exists()
    assert not project.destroy_plan_path.exists()


def test_declining_changes_nothing(tmp_path):
    project = _applied_project(tmp_path)
    tofu = FakeTofu([_resource("google_compute_network.main")])

    with pytest.raises(Declined):
        _run(project, tofu, answer=False)

    assert [c[0] for c in tofu.calls] == ["init"]
    assert project.tfplan_path.exists()


def test_never_planned_project_is_refused(tmp_path):
    project = Project(tmp_path)
    project.init()
    with pytest.raises(EasyMigrateError, match="never been planned"):
        _run(project, FakeTofu([]))


def test_missing_credentials_are_refused(tmp_path):
    project = _applied_project(tmp_path)
    with pytest.raises(EasyMigrateError, match="no GCP credentials"):
        _run(project, FakeTofu([]), FakeGcp(creds_ok=False))


def test_empty_state_needs_no_confirmation(tmp_path):
    project = _applied_project(tmp_path)
    tofu = FakeTofu([])

    result, seen = _run(project, tofu)

    assert seen == []
    assert "Nothing to destroy: the Terraform state is empty" in result.output
    assert not project.tfplan_path.exists()


def test_protected_resources_are_refused_without_the_flag(tmp_path):
    project = _applied_project(tmp_path)
    tofu = FakeTofu(
        [
            _resource("google_compute_instance.app", deletion_protection=True),
            _resource(
                "google_sql_database_instance.db",
                deletion_protection=False,
                settings=[{"deletion_protection_enabled": True}],
            ),
            _resource("google_compute_network.main"),
        ]
    )

    with pytest.raises(EasyMigrateError, match="--remove-protection") as excinfo:
        _run(project, tofu)

    assert "google_compute_instance.app" in str(excinfo.value)
    assert "google_sql_database_instance.db" in str(excinfo.value)
    assert "google_compute_network.main" not in str(excinfo.value)
    assert [c[0] for c in tofu.calls] == ["init"]


def test_remove_protection_lifts_it_in_terraform_then_destroys(tmp_path):
    tf = (
        'resource "google_compute_instance" "app" {\n'
        "  deletion_protection = true\n"
        "}\n"
        'resource "google_sql_database_instance" "db" {\n'
        "  deletion_protection = true\n"
        "  settings {\n"
        "    deletion_protection_enabled = true\n"
        '    tier                        = "db-f1-micro"\n'
        "  }\n"
        "}\n"
    )
    project = _applied_project(tmp_path, tf)
    tofu = FakeTofu(
        [
            _resource("google_compute_instance.app", deletion_protection=True),
            _resource(
                "google_sql_database_instance.db",
                deletion_protection=True,
                settings=[{"deletion_protection_enabled": True}],
            ),
        ]
    )

    result, seen = _run(project, tofu, remove_protection=True)

    assert seen[0].protected == ["google_compute_instance.app", "google_sql_database_instance.db"]
    text = (project.terraform_dir / "main.tf").read_text()
    assert "= true" not in text
    assert "deletion_protection_enabled = false" in text
    assert 'tier                        = "db-f1-micro"' in text
    names = [c[0] for c in tofu.calls]
    assert names.index("apply_targets") < names.index("plan_destroy")
    assert (
        "apply_targets",
        ("google_compute_instance.app", "google_sql_database_instance.db"),
    ) in (tofu.calls)
    assert tofu.resources == []


def test_protection_that_will_not_lift_stops_before_destroying(tmp_path):
    project = _applied_project(tmp_path)
    tofu = FakeTofu([_resource("google_compute_instance.app", deletion_protection=True)])
    tofu.apply_targets = lambda targets, stream: None  # the switch stays on

    with pytest.raises(EasyMigrateError, match="still on for: google_compute_instance.app"):
        _run(project, tofu, remove_protection=True)

    assert ("plan_destroy",) not in tofu.calls


def test_stuck_services_peering_is_removed_and_the_destroy_retried(tmp_path):
    project = _applied_project(tmp_path)
    gcp = FakeGcp()
    tofu = FakeTofu(
        [
            _resource("google_sql_database_instance.db", deletion_protection=False),
            _resource(
                "google_service_networking_connection.psa",
                network="projects/my-proj/global/networks/main-vpc",
                peering="servicenetworking-googleapis-com",
            ),
            _resource("google_compute_network.main"),
        ],
        fail_first_apply=_NETWORKING_ERROR,
    )

    result, _ = _run(project, tofu, gcp)

    assert gcp.peerings == [("my-proj", "main-vpc", "servicenetworking-googleapis-com")]
    assert result.peerings_removed == ["main-vpc/servicenetworking-googleapis-com"]
    assert [c[0] for c in tofu.calls].count("plan_destroy") == 2
    assert tofu.resources == []


def test_other_failures_are_not_retried_and_keep_the_hint(tmp_path):
    project = _applied_project(tmp_path)
    tofu = FakeTofu(
        [_resource("google_storage_bucket.site")],
        fail_first_apply="Error: googleapi: Error 409: The bucket you tried to delete is "
        "not empty.",
    )

    with pytest.raises(EasyMigrateError, match="never deletes data"):
        _run(project, tofu)

    assert [c[0] for c in tofu.calls].count("plan_destroy") == 1


def test_state_bucket_deleted_only_when_asked(tmp_path):
    project = _applied_project(tmp_path)
    gcp = FakeGcp()
    _run(project, FakeTofu([_resource("google_compute_network.main")]), gcp)
    assert gcp.deleted_buckets == []

    project = _applied_project(tmp_path)
    result, _ = _run(project, FakeTofu([]), gcp, delete_state_bucket=True)
    assert gcp.deleted_buckets == ["my-proj-easymigrate-tfstate"]
    assert result.bucket_deleted is True


def test_child_module_resources_are_included(tmp_path):
    project = _applied_project(tmp_path)
    tofu = FakeTofu([])
    tofu.state_json = lambda: {
        "values": {
            "root_module": {
                "resources": [_resource("google_compute_network.main")],
                "child_modules": [{"resources": [_resource("google_compute_subnetwork.a")]}],
            }
        }
    }
    tofu.apply = lambda plan, stream: None

    _, seen = _run(project, tofu)

    assert seen[0].addresses == ["google_compute_network.main", "google_compute_subnetwork.a"]


def test_missing_state_bucket_means_nothing_to_destroy(tmp_path):
    project = _applied_project(tmp_path)
    gcp = FakeGcp()
    gcp.bucket_exists = False
    tofu = FakeTofu([])

    result, seen = _run(project, tofu, gcp)

    assert seen == [] and tofu.calls == []
    assert (
        "Nothing to destroy: the state bucket gs://my-proj-easymigrate-tfstate does not exist"
        in (result.output)
    )
