"""project_state derives everything from files on disk (spec §5, plan 1 task 2). The
filesystem is the state: no pipeline result may be cached in server memory."""

from __future__ import annotations

import json
from pathlib import Path

from moto import mock_aws

from easymigrate.core.pipeline import analyze as run_analyze
from easymigrate.core.pipeline import resolve as run_resolve
from easymigrate.core.pipeline import scan as run_scan
from easymigrate.core.plugin import SCAN_FILE
from easymigrate.core.project import Project
from easymigrate.core.registry import source_for
from easymigrate.ui.state import project_state


def _acl_vpc(tmp_path: Path) -> Project:
    """One VPC with a custom network ACL: triggers only NET-NACL, a manual-only rule."""
    project = Project(tmp_path)
    with mock_aws():
        ec2 = source_for("aws").connect(region="ap-south-1").client("ec2")
        vpc = ec2.create_vpc(
            CidrBlock="10.0.0.0/16",
            TagSpecifications=[{"ResourceType": "vpc", "Tags": [{"Key": "Name", "Value": "main"}]}],
        )["Vpc"]["VpcId"]
        ec2.create_network_acl(
            VpcId=vpc,
            TagSpecifications=[
                {"ResourceType": "network-acl", "Tags": [{"Key": "Name", "Value": "a"}]}
            ],
        )
        run_scan(project, region="ap-south-1")
    return project


def test_no_easymigrate_dir_yields_all_stages_incomplete(tmp_path: Path):
    project = Project(tmp_path)
    state = project_state(project)
    assert len(state.stages) == 5
    assert {s.name for s in state.stages} == {"scan", "analyze", "resolve", "plan", "apply"}
    assert all(not s.complete for s in state.stages)
    assert all(s.artifact_at is None for s in state.stages)
    assert state.source is None
    assert state.target is None
    assert state.blocked == []


def test_scan_artifact_makes_scan_complete_others_not(tmp_path: Path):
    project = Project(tmp_path)
    project.init()
    snapshot_dir = project.snapshot_dir("aws")
    project.write_json(
        snapshot_dir / SCAN_FILE,
        {"account_id": "123", "region": "ap-south-1", "scanned_at": "2026-01-01T00:00:00+00:00"},
    )
    state = project_state(project)
    by_name = {s.name: s for s in state.stages}
    assert by_name["scan"].complete
    assert by_name["scan"].artifact_at is not None
    for name in ("analyze", "resolve", "plan", "apply"):
        assert not by_name[name].complete


def test_blocked_reports_unacknowledged_manual_decision(tmp_path: Path):
    project = _acl_vpc(tmp_path)
    run_analyze(project)
    run_resolve(project, accept_defaults=True)  # leaves the manual NET-NACL unacknowledged

    state = project_state(project)
    assert state.blocked
    assert any("not acknowledged" in problem for problem in state.blocked)


def test_source_and_target_populated_from_project_yaml(tmp_path: Path):
    project = _acl_vpc(tmp_path)
    state = project_state(project)
    assert state.source is not None
    assert state.source["cloud"] == "aws"
    assert state.source["region"] == "ap-south-1"
    assert state.target is None


def test_state_is_re_read_per_call_not_cached(tmp_path: Path):
    project = Project(tmp_path)
    project.init()
    before = project_state(project)
    assert not before.stages[0].complete

    snapshot_dir = project.snapshot_dir("aws")
    project.write_json(
        snapshot_dir / SCAN_FILE,
        {"account_id": "123", "region": "ap-south-1", "scanned_at": "2026-01-01T00:00:00+00:00"},
    )
    after = project_state(project)
    assert after.stages[0].complete


def test_analyze_and_plan_and_resolve_artifacts_detected(tmp_path: Path):
    project = Project(tmp_path)
    project.init()
    project.write_json(project.ir_path, {"objects": [], "findings": []})
    project.write_text(project.decisions_path, "differences_fingerprint: abc\ndecisions: []\n")
    project.write_text(project.tfplan_path, "fake-plan-bytes")

    state = project_state(project)
    by_name = {s.name: s for s in state.stages}
    assert by_name["analyze"].complete
    assert by_name["resolve"].complete
    assert by_name["plan"].complete
    assert not by_name["apply"].complete


def test_apply_complete_from_succeeded_job_record(tmp_path: Path):
    project = Project(tmp_path)
    project.init()
    jobs_dir = project.dir / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    (jobs_dir / "job1.json").write_text(
        json.dumps(
            {
                "id": "job1",
                "stage": "apply",
                "status": "succeeded",
                "started_at": "2026-01-01T00:00:00+00:00",
                "finished_at": "2026-01-01T00:10:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    state = project_state(project)
    by_name = {s.name: s for s in state.stages}
    assert by_name["apply"].complete
    assert by_name["apply"].artifact_at is not None


def test_project_state_exposes_a_running_job_as_active(tmp_path: Path):
    """spec §5: GET /api/project must carry the active job, so a closed browser tab can
    find its way back to a job in progress without already knowing its id."""
    import threading

    from easymigrate.ui.jobs import JobStore

    project = Project(tmp_path)
    store = JobStore(project)
    gate = threading.Event()
    record = store.start("scan", lambda stream: gate.wait(5))

    try:
        state = project_state(project)
        assert state.active_job is not None
        assert state.active_job.id == record.id
        assert state.active_job.status == "running"
    finally:
        gate.set()
        _wait_terminal(store, record.id)


def test_project_state_active_job_is_none_once_finished(tmp_path: Path):
    from easymigrate.ui.jobs import JobStore

    project = Project(tmp_path)
    store = JobStore(project)
    record = store.start("scan", lambda stream: None)
    _wait_terminal(store, record.id)

    state = project_state(project)

    assert state.active_job is None


def _wait_terminal(store, job_id: str, timeout: float = 5.0) -> None:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = store.get(job_id)
        if record.status != "running":
            return
        time.sleep(0.01)
    raise AssertionError("job did not reach a terminal state in time")


def test_apply_not_complete_from_failed_job_record(tmp_path: Path):
    project = Project(tmp_path)
    project.init()
    jobs_dir = project.dir / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    (jobs_dir / "job1.json").write_text(
        json.dumps(
            {
                "id": "job1",
                "stage": "apply",
                "status": "failed",
                "started_at": "2026-01-01T00:00:00+00:00",
                "finished_at": "2026-01-01T00:10:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    state = project_state(project)
    by_name = {s.name: s for s in state.stages}
    assert not by_name["apply"].complete
