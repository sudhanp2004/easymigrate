"""`easymigrate destroy` CLI tests; the pipeline is faked at the registry seams."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

import easymigrate.engine.tofu as tofu_module
from easymigrate import __version__
from easymigrate.cli import app
from easymigrate.core import registry
from easymigrate.core.project import Project, ProjectConfig, TargetConfig

runner = CliRunner()


class FakeGcp:
    deleted: list[str] = []

    bucket_exists = True

    def ensure_bucket(self, gcp_project, name, region, confirm):
        assert confirm(name) is False, "destroy must never create the state bucket"
        return self.bucket_exists

    def credentials_ok(self):
        return True

    def remove_peering(self, gcp_project, network, peering):
        raise AssertionError("no peering in this state")

    def delete_bucket(self, gcp_project, name):
        FakeGcp.deleted.append(name)
        return True


class FakeTofu:
    resources: list[dict] = []

    def __init__(self, binary=None, workdir=None):
        pass

    def init(self, backend_config=None, *, backend=True):
        pass

    def state_json(self):
        return {"values": {"root_module": {"resources": FakeTofu.resources}}}

    def plan_destroy(self, out: Path, stream):
        out.write_bytes(b"plan")

    def apply(self, plan: Path, stream):
        FakeTofu.resources = []
        stream("Destroy complete! Resources: 1 destroyed.")


def _setup(tmp_path, monkeypatch, resources):
    monkeypatch.chdir(tmp_path)
    project = Project(tmp_path)
    project.init()
    project.save_config(
        ProjectConfig(
            tool_version=__version__,
            target=TargetConfig(cloud="gcp", project="my-proj", region="asia-south1"),
        )
    )
    project.terraform_dir.mkdir(parents=True)
    (project.terraform_dir / "network.tf").write_text("")
    FakeTofu.resources = resources
    FakeGcp.deleted = []
    monkeypatch.setattr(registry, "gcp_target", lambda: FakeGcp())
    monkeypatch.setattr(tofu_module, "ensure_tofu", lambda *a, **kw: Path("/fake/tofu"))
    monkeypatch.setattr(tofu_module, "Tofu", FakeTofu)
    return project


_NETWORK = {
    "address": "google_compute_network.main",
    "mode": "managed",
    "type": "google_compute_network",
    "values": {},
}


def test_destroy_lists_resources_and_asks(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, [dict(_NETWORK)])
    monkeypatch.setattr("easymigrate.cli._stdin_is_tty", lambda: True)

    result = runner.invoke(app, ["destroy"], input="y\n")

    assert result.exit_code == 0, result.output
    assert "1 resource(s) will be deleted from my-proj" in result.output
    assert "- google_compute_network.main" in result.output
    assert "This cannot be undone" in result.output
    assert "Destroy complete: 1 resource(s) deleted." in result.output
    assert FakeTofu.resources == []


def test_destroy_declined_exits_two_and_keeps_everything(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, [dict(_NETWORK)])
    monkeypatch.setattr("easymigrate.cli._stdin_is_tty", lambda: True)

    result = runner.invoke(app, ["destroy"], input="n\n")

    assert result.exit_code == 2, result.output
    assert FakeTofu.resources != []


def test_destroy_off_a_terminal_needs_yes(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, [dict(_NETWORK)])
    monkeypatch.setattr("easymigrate.cli._stdin_is_tty", lambda: False)

    result = runner.invoke(app, ["destroy"])
    assert result.exit_code == 1
    assert "re-run with --yes" in result.output

    result = runner.invoke(app, ["destroy", "--yes", "--delete-state-bucket"])
    assert result.exit_code == 0, result.output
    assert FakeGcp.deleted == ["my-proj-easymigrate-tfstate"]


def test_destroy_names_protected_resources(tmp_path, monkeypatch):
    protected = dict(_NETWORK, address="google_compute_instance.app")
    protected["values"] = {"deletion_protection": True}
    _setup(tmp_path, monkeypatch, [protected])

    result = runner.invoke(app, ["destroy", "--yes"])

    assert result.exit_code == 1
    assert "google_compute_instance.app" in result.output
    assert "--remove-protection" in result.output
