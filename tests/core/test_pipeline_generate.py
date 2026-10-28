"""pipeline.generate(): the Terraform of plan() without GCP or OpenTofu (hosted report)."""

from __future__ import annotations

import shutil

import pytest
from moto import mock_aws

from easymigrate.core import registry
from easymigrate.core.errors import NotReady
from easymigrate.core.pipeline import analyze as run_analyze
from easymigrate.core.pipeline import generate, plan, scan
from easymigrate.core.project import Project
from tests.core.test_pipeline_plan_apply import (
    _plan_kwargs,
    _project_ready_to_plan,
)


class RecordingSource:
    def __init__(self):
        self.kwargs: dict = {}

    def connect(self, **kwargs):
        self.kwargs = kwargs
        raise RuntimeError("stop here")


@pytest.mark.parametrize("given", [True, False])
def test_scan_forwards_external_id_only_when_given(tmp_path, given):
    source = RecordingSource()
    extra = {"external_id": "e" * 32, "session_name": "easymigrate-abcd1234"} if given else {}
    with pytest.raises(RuntimeError):
        scan(Project(tmp_path), region="ap-south-1", role_arn="arn:x", source=source, **extra)

    expected = {"region": "ap-south-1", "profile": None, "role_arn": "arn:x", **extra}
    assert source.kwargs == expected


def _no_gcp(monkeypatch):
    def boom():
        raise AssertionError("generate must not touch GCP")

    monkeypatch.setattr(registry, "gcp_target", boom)
    monkeypatch.setattr(registry, "ensure_tofu", boom)


def test_generate_writes_terraform_without_gcp(tmp_path, monkeypatch):
    project = _project_ready_to_plan(tmp_path)
    _no_gcp(monkeypatch)

    summary = generate(project, gcp_project="p-123456")

    tf = project.terraform_dir
    for name in ("providers.tf", "backend.tf", "variables.tf", "terraform.tfvars"):
        assert (tf / name).exists(), name
    assert 'project = "p-123456"' in (tf / "terraform.tfvars").read_text()
    assert summary.gcp_project == "p-123456"
    assert summary.gcp_region == "asia-south1"
    assert "providers.tf" in summary.files
    assert not project.secrets_path.exists()
    assert project.resolved_ir_path.exists()
    assert project.report_md_path.exists()
    assert project.load_config().target is None


def test_generate_matches_plan_output(tmp_path, monkeypatch):
    project = _project_ready_to_plan(tmp_path / "a")
    plan(project, **_plan_kwargs(gcp_project="p-123456"))
    planned = {p.name: p.read_text() for p in project.terraform_dir.glob("*.tf")}
    planned["terraform.tfvars"] = (project.terraform_dir / "terraform.tfvars").read_text()

    shutil.rmtree(project.terraform_dir)
    _no_gcp(monkeypatch)
    generate(project, gcp_project="p-123456")
    generated = {p.name: p.read_text() for p in project.terraform_dir.glob("*.tf")}
    generated["terraform.tfvars"] = (project.terraform_dir / "terraform.tfvars").read_text()

    assert generated == planned


def test_generate_not_ready_raises(tmp_path, monkeypatch):
    project = Project(tmp_path)
    with mock_aws():
        ec2 = registry.source_for("aws").connect(region="ap-south-1").client("ec2")
        ec2.create_network_acl(VpcId=ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"])
        scan(project, region="ap-south-1")
    run_analyze(project)
    _no_gcp(monkeypatch)

    with pytest.raises(NotReady):
        generate(project, gcp_project="p-123456")


def test_generate_lists_password_variables(tmp_path, monkeypatch):
    project = _project_ready_to_plan(tmp_path)
    _no_gcp(monkeypatch)
    monkeypatch.setattr(
        registry, "database_password_variables", lambda doc: {"database_instance:db": "db_pw"}
    )

    summary = generate(project, gcp_project="p-123456")

    assert summary.password_variables == ["db_pw"]
