"""JobStore: disk-backed background jobs (spec §4, plan 2 task 1). The record file is
the truth; nothing here may be cached in server memory."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from easymigrate.core.project import Project
from easymigrate.ui.jobs import JobAlreadyRunning, JobRecord, JobStore


@pytest.fixture
def project(tmp_path: Path) -> Project:
    return Project(tmp_path)


def test_start_writes_the_record_before_work_begins(project: Project):
    store = JobStore(project)
    started = threading.Event()
    proceed = threading.Event()
    holder: dict = {}

    def work(stream):
        started.set()
        proceed.wait(5)

    record = store.start("scan", work)
    assert started.wait(5), "work never started"
    # A fresh store, reading only from disk, must already see the record while work runs.
    holder["seen"] = JobStore(project).get(record.id)
    proceed.set()
    _wait_terminal(store, record.id)

    assert holder["seen"].id == record.id
    assert holder["seen"].status == "running"


def test_successful_job_reaches_succeeded_with_log(project: Project):
    store = JobStore(project)

    def work(stream):
        stream("line one")
        stream("line two")

    record = store.start("scan", work)
    _wait_terminal(store, record.id)

    finished = store.get(record.id)
    assert finished.status == "succeeded"
    assert finished.finished_at is not None
    log_text = store.log_path(record.id).read_text(encoding="utf-8")
    assert "line one" in log_text
    assert "line two" in log_text


def test_failed_job_carries_exception_message_verbatim(project: Project):
    store = JobStore(project)

    def work(stream):
        raise ValueError("boom: something specific")

    record = store.start("plan", work)
    _wait_terminal(store, record.id)

    finished = store.get(record.id)
    assert finished.status == "failed"
    assert finished.error == "boom: something specific"
    assert finished.finished_at is not None


def test_second_start_while_active_raises(project: Project):
    store = JobStore(project)
    gate = threading.Event()

    def work(stream):
        gate.wait(5)

    store.start("scan", work)
    with pytest.raises(JobAlreadyRunning):
        store.start("plan", lambda stream: None)
    gate.set()


def test_active_returns_none_once_job_finishes(project: Project):
    store = JobStore(project)
    record = store.start("scan", lambda stream: None)
    _wait_terminal(store, record.id)

    assert store.active() is None


def test_active_returns_the_running_job(project: Project):
    store = JobStore(project)
    gate = threading.Event()
    record = store.start("scan", lambda stream: gate.wait(5))

    active = store.active()
    assert active is not None
    assert active.id == record.id
    gate.set()
    _wait_terminal(store, record.id)


def test_records_survive_a_fresh_job_store_instance(project: Project):
    store = JobStore(project)
    record = store.start("scan", lambda stream: None)
    _wait_terminal(store, record.id)

    fresh = JobStore(project)
    reread = fresh.get(record.id)
    assert reread.status == "succeeded"
    assert fresh.active() is None


def test_get_missing_job_raises_key_error(project: Project):
    store = JobStore(project)
    with pytest.raises(KeyError):
        store.get("does-not-exist")


def test_job_record_has_current_pid(project: Project):
    store = JobStore(project)
    record = store.start("scan", lambda stream: None)
    assert record.pid == os.getpid()
    _wait_terminal(store, record.id)


def _wait_terminal(store: JobStore, job_id: str, timeout: float = 5.0) -> JobRecord:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = store.get(job_id)
        if record.status != "running":
            return record
        time.sleep(0.01)
    raise AssertionError("job did not reach a terminal state in time")


def _dead_pid() -> int:
    """Spawn a subprocess and wait for it to exit: its pid is genuinely free, unlike
    `os.getpid() + 1`, which can collide with an unrelated live process on the machine."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=5)
    return proc.pid


def test_reap_stale_marks_a_different_pid_running_record_as_failed(project: Project):
    store = JobStore(project)
    dead = JobRecord(
        id="dead-job",
        stage="apply",
        status="running",
        started_at=datetime.now(UTC),
        pid=_dead_pid(),
    )
    project.write_json(store._record_path(dead.id), dead)

    reaped = store.reap_stale()

    assert [r.id for r in reaped] == ["dead-job"]
    updated = store.get("dead-job")
    assert updated.status == "failed"
    assert updated.finished_at is not None
    assert "the EasyMigrate server stopped while this apply was running" in updated.error
    assert "its result is unknown" in updated.error


def test_reap_stale_leaves_a_live_foreign_pid_running_record_alone(project: Project):
    """A record's pid differing from ours is not enough: a second `easymigrate ui`
    started against the same project must not reap the first server's still-running
    job. Only a pid that is both foreign and actually dead may be reaped."""
    store = JobStore(project)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        alive = JobRecord(
            id="alive-job",
            stage="apply",
            status="running",
            started_at=datetime.now(UTC),
            pid=proc.pid,
        )
        project.write_json(store._record_path(alive.id), alive)

        reaped = store.reap_stale()

        assert reaped == []
        assert store.get("alive-job").status == "running"
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_reap_stale_leaves_succeeded_records_alone(project: Project):
    store = JobStore(project)
    record = store.start("scan", lambda stream: None)
    _wait_terminal(store, record.id)

    reaped = store.reap_stale()

    assert reaped == []
    assert store.get(record.id).status == "succeeded"


def test_reap_stale_leaves_this_process_own_running_job_alone(project: Project):
    store = JobStore(project)
    gate = threading.Event()
    record = store.start("scan", lambda stream: gate.wait(5))

    reaped = store.reap_stale()

    assert reaped == []
    assert store.get(record.id).status == "running"
    gate.set()
    _wait_terminal(store, record.id)
