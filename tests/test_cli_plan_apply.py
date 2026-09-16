"""Tests for `easymigrate plan` and `easymigrate apply` (plan 4 B4/B6). GCP and
OpenTofu are faked through the documented seams: registry.gcp_target and
easymigrate.engine.tofu.ensure_tofu/Tofu (a module-attribute seam, so patching the
attribute on the module object is enough even though pipeline reaches it through
registry). Nothing here touches real GCP or downloads a real OpenTofu binary."""

from __future__ import annotations

from pathlib import Path

from moto import mock_aws
from typer.testing import CliRunner

import easymigrate.engine.tofu as tofu_module
from easymigrate.cli import app
from easymigrate.core import registry
from easymigrate.core.pipeline import analyze as run_analyze
from easymigrate.core.pipeline import resolve as run_resolve
from easymigrate.core.pipeline import scan
from easymigrate.core.project import Project
from easymigrate.core.registry import source_for
from easymigrate.ir.base import Finding

runner = CliRunner()


class FakeGcp:
    def __init__(self, *, creds_ok=True, bucket_exists=True):
        self.creds_ok = creds_ok
        self.bucket_exists = bucket_exists

    def credentials_ok(self):
        return self.creds_ok

    def ensure_bucket(self, gcp_project, name, region, confirm):
        if self.bucket_exists:
            return True
        return confirm(name)


class FakeTofu:
    def __init__(self, binary=None, workdir=None):
        self.workdir = workdir

    def init(self, backend_config=None, *, backend=True):
        pass

    def validate(self):
        pass

    def plan(self, out: Path) -> str:
        out.write_bytes(b"fake-plan")
        return "Plan: 1 to add."

    def show_json(self, plan: Path) -> dict:
        return {
            "resource_changes": [
                {"address": "google_compute_network.main", "change": {"actions": ["create"]}}
            ]
        }

    def apply(self, plan: Path, stream) -> None:
        stream("apply: complete")


def _ready_project(tmp_path) -> Project:
    project = Project(tmp_path)
    with mock_aws():
        ec2 = source_for("aws").connect(region="ap-south-1").client("ec2")
        vpc = ec2.create_vpc(
            CidrBlock="10.0.0.0/16",
            TagSpecifications=[{"ResourceType": "vpc", "Tags": [{"Key": "Name", "Value": "main"}]}],
        )["Vpc"]["VpcId"]
        ec2.create_subnet(VpcId=vpc, CidrBlock="10.0.1.0/24", AvailabilityZone="ap-south-1a")
        scan(project, region="ap-south-1")
    run_analyze(project)
    run_resolve(project, accept_defaults=True)
    return project


def _patch_tofu(monkeypatch):
    monkeypatch.setattr(tofu_module, "ensure_tofu", lambda *a, **kw: Path("/fake/tofu"))
    monkeypatch.setattr(tofu_module, "Tofu", FakeTofu)


def test_plan_command_writes_terraform_and_prints_summary(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project = _ready_project(tmp_path)
    monkeypatch.setattr(registry, "gcp_target", lambda: FakeGcp())
    _patch_tofu(monkeypatch)

    result = runner.invoke(app, ["plan", "--gcp-project", "my-proj"])

    assert result.exit_code == 0, result.output
    assert "Plan" in result.output
    assert (project.terraform_dir / "network.tf").exists()
    assert "Next: run `easymigrate apply`." in result.output


def test_plan_command_prints_non_info_findings(tmp_path, monkeypatch):
    """A non-info finding reaching PlanSummary.findings (here: an `unsupported` finding
    carried through from analyze) must be printed by `plan`, not just written to
    plan_summary.json/report.md (spec D2: nothing is dropped silently)."""
    monkeypatch.chdir(tmp_path)
    project = _ready_project(tmp_path)
    ir_doc = project.read_ir(project.ir_path, produced_by="analyze")
    extra = Finding(
        level="unsupported",
        code="TEST-UNSUPPORTED",
        message="some-service resource(s) found that EasyMigrate does not migrate yet",
        subject="test:resource",
    )
    project.write_ir(project.ir_path, ir_doc.model_copy(update={"findings": [extra]}))
    monkeypatch.setattr(registry, "gcp_target", lambda: FakeGcp())
    _patch_tofu(monkeypatch)

    result = runner.invoke(app, ["plan", "--gcp-project", "my-proj"])

    assert result.exit_code == 0, result.output
    assert "TEST-UNSUPPORTED" in result.output
    assert extra.message in result.output


def test_plan_command_prompts_for_missing_bucket(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _ready_project(tmp_path)
    monkeypatch.setattr(registry, "gcp_target", lambda: FakeGcp(bucket_exists=False))
    _patch_tofu(monkeypatch)

    result = runner.invoke(app, ["plan", "--gcp-project", "my-proj"], input="y\n")

    assert result.exit_code == 0, result.output
    assert "does not exist. Create it in" in result.output


def test_plan_command_declined_bucket_exits_two(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _ready_project(tmp_path)
    monkeypatch.setattr(registry, "gcp_target", lambda: FakeGcp(bucket_exists=False))
    _patch_tofu(monkeypatch)

    result = runner.invoke(app, ["plan", "--gcp-project", "my-proj"], input="n\n")

    assert result.exit_code == 2


def test_apply_command_confirmed_runs_and_streams_output(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project = _ready_project(tmp_path)
    monkeypatch.setattr(registry, "gcp_target", lambda: FakeGcp())
    _patch_tofu(monkeypatch)
    plan_result = runner.invoke(app, ["plan", "--gcp-project", "my-proj"])
    assert plan_result.exit_code == 0, plan_result.output

    result = runner.invoke(app, ["apply"], input="y\n")

    assert result.exit_code == 0, result.output
    assert "Apply these changes to project my-proj" in result.output
    assert "apply: complete" in result.output
    assert project.report_md_path.exists()


def test_apply_command_declined_exits_two(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _ready_project(tmp_path)
    monkeypatch.setattr(registry, "gcp_target", lambda: FakeGcp())
    _patch_tofu(monkeypatch)
    runner.invoke(app, ["plan", "--gcp-project", "my-proj"])

    result = runner.invoke(app, ["apply"], input="n\n")

    assert result.exit_code == 2


def test_apply_command_without_plan_exits_two(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _ready_project(tmp_path)

    result = runner.invoke(app, ["apply"], input="y\n")

    assert result.exit_code == 2
