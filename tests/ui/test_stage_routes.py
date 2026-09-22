"""POST /api/scan, /api/plan, /api/apply: each starts a background job (spec §3, §4,
§5, plan 2 task 3). GCP and OpenTofu are faked through the same documented seams as
tests/core/test_pipeline_plan_apply.py: registry.gcp_target/ensure_tofu/new_tofu."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from easymigrate.core import registry
from easymigrate.core.pipeline import analyze as run_analyze
from easymigrate.core.pipeline import resolve as run_resolve
from easymigrate.core.pipeline import scan as run_scan
from easymigrate.core.project import Project
from easymigrate.core.registry import source_for
from easymigrate.ui.app import create_app
from easymigrate.ui.routes.stages import plan_fingerprint
from easymigrate.ui.security import Security

PORT = 54744
TOKEN = "stage-test-token"
HEADERS = {"Host": f"127.0.0.1:{PORT}", "X-EasyMigrate-Token": TOKEN}


class FakeGcp:
    def __init__(self, *, bucket_exists: bool = True, services_result: object = None):
        self.bucket_exists = bucket_exists
        self.services_result = services_result
        self.ensure_bucket_calls: list[tuple] = []

    def credentials_ok(self) -> bool:
        return True

    def ensure_bucket(self, gcp_project, name, region, confirm):
        self.ensure_bucket_calls.append((gcp_project, name, region))
        if self.bucket_exists:
            return True
        return bool(confirm(name))

    def services_enabled(self, gcp_project, required):
        return self.services_result


class FakeTofu:
    def __init__(self, show_json=None, raise_on_apply=None):
        self.init_calls: list[dict] = []
        self._show_json = show_json or {"resource_changes": []}
        self._raise_on_apply = raise_on_apply

    def init(self, backend_config=None, *, backend=True):
        self.init_calls.append(backend_config or {})

    def plan(self, out: Path, stream=None) -> str:
        out.write_bytes(b"fake-plan-bytes")
        text = "Plan: 1 to add, 0 to change, 0 to destroy."
        if stream is not None:
            stream(text)
        return text

    def show_json(self, plan: Path) -> dict:
        return self._show_json

    def apply(self, plan: Path, stream) -> None:
        if self._raise_on_apply is not None:
            raise self._raise_on_apply
        stream("apply complete")


@pytest.fixture
def project(tmp_path: Path) -> Project:
    return Project(tmp_path)


@pytest.fixture
def security() -> Security:
    return Security(token=TOKEN, host="127.0.0.1", port=PORT)


@pytest.fixture
def client(project: Project, security: Security) -> TestClient:
    return TestClient(create_app(project, security))


def _scanned_and_resolved(project: Project) -> None:
    with mock_aws():
        ec2 = source_for("aws").connect(region="ap-south-1").client("ec2")
        vpc = ec2.create_vpc(
            CidrBlock="10.0.0.0/16",
            TagSpecifications=[{"ResourceType": "vpc", "Tags": [{"Key": "Name", "Value": "main"}]}],
        )["Vpc"]["VpcId"]
        ec2.create_subnet(VpcId=vpc, CidrBlock="10.0.1.0/24", AvailabilityZone="ap-south-1a")
        run_scan(project, region="ap-south-1")
    run_analyze(project)
    run_resolve(project, accept_defaults=True)


def _scanned_and_resolved_with_database(project: Project) -> str:
    """Like `_scanned_and_resolved`, but with a real RDS instance in the snapshot, so a
    supplied password is actually consumed by `_resolve_db_passwords` rather than the
    engine finding nothing to do with it. Returns the instance's IR id."""
    with mock_aws():
        clients = source_for("aws").connect(region="ap-south-1")
        ec2 = clients.client("ec2")
        rds = clients.client("rds")
        vpc = ec2.create_vpc(
            CidrBlock="10.0.0.0/16",
            TagSpecifications=[{"ResourceType": "vpc", "Tags": [{"Key": "Name", "Value": "main"}]}],
        )["Vpc"]["VpcId"]
        subnet_a = ec2.create_subnet(
            VpcId=vpc, CidrBlock="10.0.1.0/24", AvailabilityZone="ap-south-1a"
        )["Subnet"]["SubnetId"]
        subnet_b = ec2.create_subnet(
            VpcId=vpc, CidrBlock="10.0.2.0/24", AvailabilityZone="ap-south-1b"
        )["Subnet"]["SubnetId"]
        sg = ec2.create_security_group(GroupName="db-sg", Description="db", VpcId=vpc)["GroupId"]
        rds.create_db_subnet_group(
            DBSubnetGroupName="subgrp1",
            DBSubnetGroupDescription="subgrp1",
            SubnetIds=[subnet_a, subnet_b],
        )
        rds.create_db_instance(
            DBInstanceIdentifier="orders-db",
            Engine="postgres",
            EngineVersion="15.5",
            DBInstanceClass="db.t3.micro",
            AllocatedStorage=20,
            MasterUsername="postgres",
            MasterUserPassword="placeholder-not-the-sentinel",
            DBSubnetGroupName="subgrp1",
            VpcSecurityGroupIds=[sg],
        )
        run_scan(project, region="ap-south-1")
    run_analyze(project)
    run_resolve(project, accept_defaults=True)

    ir_doc = json.loads(project.ir_path.read_text(encoding="utf-8"))
    db_id = next(obj["id"] for obj in ir_doc["objects"] if obj["kind"] == "database_instance")
    return db_id


def _wait_for_terminal(client: TestClient, job_id: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/api/jobs/{job_id}", headers=HEADERS).json()
        if body["status"] != "running":
            return body
        time.sleep(0.01)
    raise AssertionError("job did not reach a terminal state in time")


def _patch_gcp_and_tofu(monkeypatch, gcp=None, tofu=None):
    gcp = gcp if gcp is not None else FakeGcp()
    tofu = tofu if tofu is not None else FakeTofu(show_json=_ADD_ONE)
    monkeypatch.setattr(registry, "gcp_target", lambda: gcp)
    monkeypatch.setattr(registry, "ensure_tofu", lambda cache_dir=None: Path("/fake/tofu"))
    monkeypatch.setattr(registry, "new_tofu", lambda binary, workdir: tofu)
    return gcp, tofu


_ADD_ONE = {
    "resource_changes": [
        {"address": "google_compute_network.main", "change": {"actions": ["create"]}},
    ]
}


# --- /api/scan ----------------------------------------------------------------------


def test_scan_returns_202_and_job_reaches_succeeded(client: TestClient, project: Project):
    with mock_aws():
        ec2 = source_for("aws").connect(region="ap-south-1").client("ec2")
        ec2.create_vpc(CidrBlock="10.0.0.0/16")

        resp = client.post("/api/scan", json={"region": "ap-south-1"}, headers=HEADERS)
        assert resp.status_code == 202
        job = resp.json()
        assert job["stage"] == "scan"
        assert job["status"] == "running"

        finished = _wait_for_terminal(client, job["id"])
    assert finished["status"] == "succeeded"


def test_second_scan_while_active_is_409(client: TestClient, project: Project):
    with mock_aws():
        source_for("aws").connect(region="ap-south-1").client("ec2").create_vpc(
            CidrBlock="10.0.0.0/16"
        )
        first = client.post("/api/scan", json={"region": "ap-south-1"}, headers=HEADERS)
        assert first.status_code == 202
        second = client.post("/api/scan", json={"region": "ap-south-1"}, headers=HEADERS)
        assert second.status_code == 409
        _wait_for_terminal(client, first.json()["id"])


# --- /api/plan ------------------------------------------------------------------------


def test_plan_returns_202_and_job_reaches_succeeded(
    client: TestClient, project: Project, monkeypatch
):
    _scanned_and_resolved(project)
    _patch_gcp_and_tofu(monkeypatch)

    resp = client.post(
        "/api/plan",
        json={"gcp_project": "my-proj", "create_bucket": True},
        headers=HEADERS,
    )
    assert resp.status_code == 202
    finished = _wait_for_terminal(client, resp.json()["id"])
    assert finished["status"] == "succeeded"
    assert project.plan_lock_path.exists()
    # The plan's progress and tofu output reach the job log; the route used to drop them.
    log = (project.jobs_dir / f"{finished['id']}.log").read_text()
    assert "Running tofu plan" in log
    assert "Plan: 1 to add" in log


def test_plan_passes_passwords_and_none_ask_password_through(
    client: TestClient, project: Project, monkeypatch
):
    """Fake the pipeline call itself to capture the kwargs `/api/plan` passes through."""
    _scanned_and_resolved(project)
    captured = {}

    def fake_plan(project_arg, **kwargs):
        captured.update(kwargs)

        class _Summary:
            plan_text_path = project.plan_txt_path

        return _Summary()

    from easymigrate.ui.routes import stages as stages_module

    monkeypatch.setattr(stages_module.pipeline, "plan", fake_plan)

    resp = client.post(
        "/api/plan",
        json={
            "gcp_project": "my-proj",
            "db_passwords": {"db-1": "sentinel-pw"},
            "create_bucket": True,
        },
        headers=HEADERS,
    )
    assert resp.status_code == 202
    _wait_for_terminal(client, resp.json()["id"])

    assert captured["db_passwords"] == {"db-1": "sentinel-pw"}
    assert captured["ask_password"] is None
    assert captured["confirm_bucket"]("any-bucket-name") is True


def test_plan_create_bucket_false_and_missing_bucket_fails_with_declined_message(
    client: TestClient, project: Project, monkeypatch
):
    _scanned_and_resolved(project)
    _patch_gcp_and_tofu(monkeypatch, gcp=FakeGcp(bucket_exists=False))

    resp = client.post(
        "/api/plan",
        json={"gcp_project": "my-proj", "create_bucket": False},
        headers=HEADERS,
    )
    assert resp.status_code == 202
    finished = _wait_for_terminal(client, resp.json()["id"])
    assert finished["status"] == "failed"
    assert "creating it was not approved" in finished["error"]


# --- /api/apply -----------------------------------------------------------------------


def _planned_project(project: Project, monkeypatch) -> tuple[FakeGcp, FakeTofu]:
    _scanned_and_resolved(project)
    gcp, tofu = _patch_gcp_and_tofu(monkeypatch)
    from easymigrate.core.pipeline import plan as run_plan

    run_plan(
        project,
        gcp_project="my-proj",
        confirm_bucket=lambda _name: True,
        gcp=gcp,
        tofu=tofu,
    )
    return gcp, tofu


def test_apply_stale_fingerprint_is_409(client: TestClient, project: Project, monkeypatch):
    _planned_project(project, monkeypatch)

    resp = client.post("/api/apply", json={"plan_fingerprint": "not-the-real-one"}, headers=HEADERS)
    assert resp.status_code == 409
    assert "plan changed" in resp.json()["detail"]


def test_apply_correct_fingerprint_returns_202_and_succeeds(
    client: TestClient, project: Project, monkeypatch
):
    _, tofu = _planned_project(project, monkeypatch)
    fingerprint = plan_fingerprint(project)

    resp = client.post("/api/apply", json={"plan_fingerprint": fingerprint}, headers=HEADERS)
    assert resp.status_code == 202
    finished = _wait_for_terminal(client, resp.json()["id"])
    assert finished["status"] == "succeeded"
    assert tofu.init_calls  # apply reuses the already-planned terraform dir


def test_apply_with_no_plan_yet_is_409(client: TestClient, project: Project):
    resp = client.post("/api/apply", json={"plan_fingerprint": "anything"}, headers=HEADERS)
    assert resp.status_code == 409


# --- password leak guard --------------------------------------------------------------


def test_password_never_appears_in_job_record_log_or_response(
    client: TestClient, project: Project, monkeypatch
):
    """A real database instance is required here: without one, `db_passwords` is never
    consumed by `_resolve_db_passwords` at all, and the assertions below would hold even
    if the route leaked the value verbatim — a guard that can't fail is not a guard."""
    sentinel = "SENTINEL-DB-PASSWORD-abc123"
    db_id = _scanned_and_resolved_with_database(project)
    _patch_gcp_and_tofu(monkeypatch)

    resp = client.post(
        "/api/plan",
        json={
            "gcp_project": "my-proj",
            "db_passwords": {db_id: sentinel},
            "create_bucket": True,
        },
        headers=HEADERS,
    )
    assert sentinel not in resp.text
    job_id = resp.json()["id"]
    finished_resp = None
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        finished_resp = client.get(f"/api/jobs/{job_id}", headers=HEADERS)
        if finished_resp.json()["status"] != "running":
            break
        time.sleep(0.01)
    assert finished_resp is not None
    assert finished_resp.json()["status"] == "succeeded"
    assert sentinel not in finished_resp.text

    jobs_dir = project.jobs_dir
    for path in jobs_dir.glob("*.json"):
        assert sentinel not in path.read_text(encoding="utf-8")
    for path in jobs_dir.glob("*.log"):
        assert sentinel not in path.read_text(encoding="utf-8")
    if project.report_json_path.exists():
        assert sentinel not in project.report_json_path.read_text(encoding="utf-8")
    if project.plan_txt_path.exists():
        assert sentinel not in project.plan_txt_path.read_text(encoding="utf-8")
