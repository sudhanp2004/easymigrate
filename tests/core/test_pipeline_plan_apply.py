"""plan()/apply() tests (plan 4 B2/B3/B6). All GCP and OpenTofu access is faked
through the documented seams; nothing here touches real GCP or downloads a real
OpenTofu binary."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from moto import mock_aws

from easymigrate.core.errors import Declined, NotReady, PlanStale
from easymigrate.core.pipeline import ApplyResult, PlanSummary, apply, plan, scan
from easymigrate.core.pipeline import analyze as run_analyze
from easymigrate.core.pipeline import resolve as run_resolve
from easymigrate.core.project import Project
from easymigrate.core.registry import source_for
from easymigrate.core.report import build_report


class FakeGcp:
    def __init__(
        self,
        *,
        creds_ok: bool = True,
        bucket_exists: bool = False,
        confirm_result=None,
        services_result: object = "unused",
    ):
        self.creds_ok = creds_ok
        self.bucket_exists = bucket_exists
        self.confirm_result = confirm_result
        self.ensure_bucket_calls: list[tuple] = []
        # `services_result` is only consulted if `services_enabled` is actually called
        # (i.e. `_required_gcp_services` returned something); the sentinel default
        # makes a stray call in a test that doesn't expect one fail loudly.
        self.services_result = services_result
        self.services_enabled_calls: list[tuple] = []

    def credentials_ok(self) -> bool:
        return self.creds_ok

    def ensure_bucket(self, gcp_project, name, region, confirm):
        self.ensure_bucket_calls.append((gcp_project, name, region))
        if self.bucket_exists:
            return True
        result = confirm(name) if self.confirm_result is None else self.confirm_result
        return bool(result)

    def services_enabled(self, gcp_project, required):
        self.services_enabled_calls.append((gcp_project, frozenset(required)))
        if self.services_result == "unused":
            raise AssertionError("services_enabled called without a configured result")
        return self.services_result


class FakeTofu:
    def __init__(self, show_json=None, raise_on_apply=None):
        self.init_calls: list[dict] = []
        self.plan_calls: list[Path] = []
        self.apply_calls: list[Path] = []
        self._show_json = show_json or {"resource_changes": []}
        self._raise_on_apply = raise_on_apply

    def init(self, backend_config=None, *, backend=True):
        self.init_calls.append(backend_config or {})

    def validate(self):
        pass

    def plan(self, out: Path) -> str:
        self.plan_calls.append(out)
        out.write_bytes(b"fake-plan-bytes")
        return "Plan: 1 to add, 0 to change, 0 to destroy."

    def show_json(self, plan: Path) -> dict:
        return self._show_json

    def apply(self, plan: Path, stream) -> None:
        self.apply_calls.append(plan)
        if self._raise_on_apply is not None:
            raise self._raise_on_apply
        stream("apply complete")


def _project_ready_to_plan(tmp_path) -> Project:
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


_ADD_ONE = {
    "resource_changes": [
        {"address": "google_compute_network.main", "change": {"actions": ["create"]}},
    ]
}


def _plan_kwargs(**overrides):
    kwargs = dict(
        gcp_project="my-proj",
        gcp_region=None,
        confirm_bucket=lambda name: True,
        gcp=FakeGcp(),
        tofu=FakeTofu(show_json=_ADD_ONE),
    )
    kwargs.update(overrides)
    return kwargs


# --- plan() -----------------------------------------------------------------------


def test_plan_not_ready_exits_with_problems(tmp_path):
    project = Project(tmp_path)
    with mock_aws():
        ec2 = source_for("aws").connect(region="ap-south-1").client("ec2")
        ec2.create_network_acl(VpcId=ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"])
        scan(project, region="ap-south-1")
    run_analyze(project)
    # No resolve at all: decisions.yaml missing -> not ready.

    with pytest.raises(NotReady) as exc_info:
        plan(project, **_plan_kwargs())

    assert exc_info.value.problems


def test_plan_unknown_region_without_override_raises(tmp_path):
    project = _project_ready_to_plan(tmp_path)
    config = project.load_config()
    project.save_config(
        config.model_copy(
            update={"source": config.source.model_copy(update={"region": "mars-north-1"})}
        )
    )

    with pytest.raises(Exception, match="no GCP region mapping"):
        plan(**_plan_kwargs(project=project))


def test_plan_bucket_declined_exits_two(tmp_path):
    project = _project_ready_to_plan(tmp_path)

    with pytest.raises(Declined):
        plan(**_plan_kwargs(project=project, confirm_bucket=lambda name: False, gcp=FakeGcp()))


def test_plan_bucket_created_when_confirmed(tmp_path):
    project = _project_ready_to_plan(tmp_path)
    gcp = FakeGcp()

    plan(**_plan_kwargs(project=project, gcp=gcp, confirm_bucket=lambda name: True))

    assert gcp.ensure_bucket_calls == [("my-proj", "my-proj-easymigrate-tfstate", "asia-south1")]


def test_plan_removes_stale_tf_files(tmp_path):
    project = _project_ready_to_plan(tmp_path)
    project.terraform_dir.mkdir(parents=True, exist_ok=True)
    stale = project.terraform_dir / "stale.tf"
    stale.write_text("# old\n")

    plan(**_plan_kwargs(project=project))

    assert not stale.exists()
    assert (project.terraform_dir / "network.tf").exists()
    assert (project.terraform_dir / "providers.tf").exists()


def test_plan_lock_contains_every_hash(tmp_path):
    project = _project_ready_to_plan(tmp_path)

    plan(**_plan_kwargs(project=project))

    lock = json.loads(project.plan_lock_path.read_text())
    assert lock["gcp_project"] == "my-proj"
    assert lock["gcp_region"] == "asia-south1"
    assert lock["hashes"]["ir.json"]
    assert lock["hashes"]["decisions.yaml"]
    assert lock["hashes"]["resolved_ir.json"]
    assert lock["hashes"]["tfplan"]
    assert "network.tf" in lock["hashes"]["terraform"]
    assert "providers.tf" in lock["hashes"]["terraform"]


def _merge_and_isolation_project(tmp_path) -> Project:
    """One network with a public and an isolated subnet: with NET-ZONE-REGIONAL resolved
    as `merge_per_network`, the isolated subnet is merged away, so NET-ISOLATION-VM-LEVEL's
    `no_external_ip` decision on it becomes a genuine superseded decision (same scenario as
    tests/differences/test_engine.py and tests/core/test_pipeline_resolve.py)."""
    project = Project(tmp_path)
    with mock_aws():
        ec2 = source_for("aws").connect(region="ap-south-1").client("ec2")
        vpc = ec2.create_vpc(
            CidrBlock="10.0.0.0/16",
            TagSpecifications=[{"ResourceType": "vpc", "Tags": [{"Key": "Name", "Value": "main"}]}],
        )["Vpc"]["VpcId"]
        igw = ec2.create_internet_gateway()["InternetGateway"]["InternetGatewayId"]
        ec2.attach_internet_gateway(InternetGatewayId=igw, VpcId=vpc)
        public = ec2.create_subnet(
            VpcId=vpc, CidrBlock="10.0.1.0/24", AvailabilityZone="ap-south-1a"
        )["Subnet"]["SubnetId"]
        ec2.create_subnet(VpcId=vpc, CidrBlock="10.0.2.0/24", AvailabilityZone="ap-south-1b")
        public_rt = ec2.create_route_table(VpcId=vpc)["RouteTable"]["RouteTableId"]
        ec2.create_route(RouteTableId=public_rt, DestinationCidrBlock="0.0.0.0/0", GatewayId=igw)
        ec2.associate_route_table(RouteTableId=public_rt, SubnetId=public)
        scan(project, region="ap-south-1")
    run_analyze(project)

    def choose(view):
        if view["difference"].rule_id == "NET-ZONE-REGIONAL":
            return "merge_per_network", False
        return view["current_strategy"], False

    resolve_summary = run_resolve(project, choose=choose)
    assert resolve_summary.problems == []
    return project


def test_plan_superseded_carried_into_summary(tmp_path):
    project = _merge_and_isolation_project(tmp_path)

    summary = plan(**_plan_kwargs(project=project))

    assert isinstance(summary, PlanSummary)
    assert len(summary.superseded) == 1
    entry = summary.superseded[0]
    assert entry.strategy == "no_external_ip"
    assert "merge_per_network" in entry.message

    report = build_report(project)
    assert any(entry.message == item for item in report.needs_action)


def test_plan_preflight_before_bucket_creation(tmp_path, monkeypatch):
    """OpenTofu preflight (spec §5.4) must run before the state bucket is created, so a
    run that can't get OpenTofu never leaves a real bucket behind."""
    from easymigrate.core import registry

    project = _project_ready_to_plan(tmp_path)
    gcp = FakeGcp()

    def _boom(cache_dir=None):
        raise RuntimeError("could not download OpenTofu")

    monkeypatch.setattr(registry, "ensure_tofu", _boom)

    with pytest.raises(RuntimeError, match="could not download OpenTofu"):
        plan(**_plan_kwargs(project=project, gcp=gcp, tofu=None))

    assert gcp.ensure_bucket_calls == []


def test_plan_missing_gcp_service_raises_with_gcloud_command(tmp_path, monkeypatch):
    """spec §6.4: a required API that isn't enabled is a hard error naming the exact
    `gcloud services enable` command, not a warning (I2: this branch had no test, so
    reverting the raise to a warning broke nothing)."""
    from easymigrate.core import pipeline

    project = _project_ready_to_plan(tmp_path)
    monkeypatch.setattr(
        pipeline, "_required_gcp_services", lambda document: {"sqladmin.googleapis.com"}
    )
    gcp = FakeGcp(services_result={"sqladmin.googleapis.com"})

    with pytest.raises(Exception, match="gcloud services enable sqladmin.googleapis.com") as exc:
        plan(**_plan_kwargs(project=project, gcp=gcp))

    assert "my-proj" in str(exc.value)
    assert gcp.services_enabled_calls == [("my-proj", frozenset({"sqladmin.googleapis.com"}))]
    # the hard error must land before any state bucket is created
    assert gcp.ensure_bucket_calls == []


def test_plan_service_check_failure_is_a_warning_and_run_continues(tmp_path, monkeypatch):
    """A failure to *check* (no permission to read Service Usage) must not block a run
    whose APIs are already enabled: it is a GCP-SERVICES-CHECK-FAILED info finding, and
    `plan()` proceeds to create the bucket and run tofu plan."""
    from easymigrate.core import pipeline

    project = _project_ready_to_plan(tmp_path)
    monkeypatch.setattr(
        pipeline, "_required_gcp_services", lambda document: {"sqladmin.googleapis.com"}
    )
    gcp = FakeGcp(services_result=None)

    summary = plan(**_plan_kwargs(project=project, gcp=gcp))

    assert gcp.services_enabled_calls == [("my-proj", frozenset({"sqladmin.googleapis.com"}))]
    assert gcp.ensure_bucket_calls, "run must continue past a check failure"
    assert any(
        f.code == "GCP-SERVICES-CHECK-FAILED" and f.level == "info" for f in summary.findings
    )


def test_plan_records_init_backend_config_and_plan_path(tmp_path):
    project = _project_ready_to_plan(tmp_path)
    tofu = FakeTofu(show_json=_ADD_ONE)

    summary = plan(**_plan_kwargs(project=project, tofu=tofu))

    assert tofu.init_calls == [{"bucket": "my-proj-easymigrate-tfstate", "prefix": "easymigrate"}]
    assert tofu.plan_calls == [project.tfplan_path]
    assert summary.plan_text_path == project.plan_txt_path
    assert summary.add == 1
    assert project.plan_txt_path.exists()


# --- apply() ------------------------------------------------------------------------


def test_apply_lock_mismatch_after_editing_terraform_exits_two(tmp_path):
    project = _project_ready_to_plan(tmp_path)
    plan(**_plan_kwargs(project=project))
    (project.terraform_dir / "network.tf").write_text("# tampered\n")

    with pytest.raises(PlanStale):
        apply(project, confirm=lambda s: True, stream=lambda line: None)


def test_apply_lock_mismatch_after_editing_tfvars_exits_two(tmp_path):
    """terraform.tfvars carries the GCP project and region (spec §5.4 step 6: every file
    in terraform/ is hashed), so editing it must also make the plan stale."""
    project = _project_ready_to_plan(tmp_path)
    plan(**_plan_kwargs(project=project))
    tfvars = project.terraform_dir / "terraform.tfvars"
    assert tfvars.exists()
    tfvars.write_text(tfvars.read_text() + "\n# tampered\n")

    with pytest.raises(PlanStale):
        apply(project, confirm=lambda s: True, stream=lambda line: None)


def test_apply_missing_tfplan_exits_two(tmp_path):
    project = _project_ready_to_plan(tmp_path)

    with pytest.raises(PlanStale):
        apply(project, confirm=lambda s: True, stream=lambda line: None)


def test_apply_declined_exits_two_and_tofu_apply_not_called(tmp_path):
    project = _project_ready_to_plan(tmp_path)
    plan(**_plan_kwargs(project=project))
    tofu = FakeTofu()

    with pytest.raises(Declined):
        apply(project, confirm=lambda s: False, stream=lambda line: None, tofu=tofu)

    assert tofu.apply_calls == []


def test_apply_confirmed_calls_tofu_apply_with_tfplan_and_streams_output(tmp_path):
    project = _project_ready_to_plan(tmp_path)
    plan(**_plan_kwargs(project=project))
    tofu = FakeTofu()
    streamed: list[str] = []

    result = apply(project, confirm=lambda s: True, stream=streamed.append, tofu=tofu)

    assert isinstance(result, ApplyResult)
    assert tofu.apply_calls == [project.tfplan_path]
    assert "apply complete" in streamed


def test_apply_tofu_error_exits_one_with_recovery_message(tmp_path):
    from easymigrate.core.errors import EasyMigrateError
    from easymigrate.engine.tofu import TofuError

    project = _project_ready_to_plan(tmp_path)
    plan(**_plan_kwargs(project=project))
    tofu = FakeTofu(raise_on_apply=TofuError("boom: real tofu stderr"))

    with pytest.raises(EasyMigrateError, match="state is safe") as exc_info:
        apply(project, confirm=lambda s: True, stream=lambda line: None, tofu=tofu)

    assert "boom: real tofu stderr" in str(exc_info.value)


def test_a_gke_cluster_requires_the_kubernetes_engine_api():
    from easymigrate.core.pipeline import _required_gcp_services
    from easymigrate.ir.base import IRDocument, SourceRef
    from easymigrate.ir.kubernetes import KubernetesCluster

    cluster = KubernetesCluster(
        id="kubernetes_cluster:prod",
        name="prod",
        source=SourceRef(cloud="aws", type="eks-cluster", id="prod", region="ap-south-1"),
        version="1.31",
    )
    doc = IRDocument(source={"cloud": "aws"}, objects=[cluster])
    assert _required_gcp_services(doc) == {"container.googleapis.com"}
    excluded = cluster.model_copy(update={"excluded": "manual"})
    assert _required_gcp_services(IRDocument(source={"cloud": "aws"}, objects=[excluded])) == set()
