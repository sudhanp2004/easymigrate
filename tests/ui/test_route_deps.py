"""Review and job routes resolve their project per request (hosted report, Task 2)."""

from __future__ import annotations

import time

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from easymigrate.core.project import Project
from easymigrate.ui.jobs import JobRecord, JobStore
from easymigrate.ui.routes import jobs as job_routes
from easymigrate.ui.routes import review as review_routes
from easymigrate.ui.routes.deps import RouteDeps
from tests.core.test_pipeline_plan_apply import _project_ready_to_plan


def test_review_routes_use_per_request_project(tmp_path):
    projects = {"a": _project_ready_to_plan(tmp_path / "a"), "b": Project(tmp_path / "b")}
    projects["b"].init()

    def pick(request: Request) -> Project:
        return projects[request.headers["x-p"]]

    def store(request: Request) -> JobStore:
        return JobStore(pick(request))

    app = FastAPI()
    deps = RouteDeps(project=pick, jobs=store, reads=[], mutating=[])
    review_routes.register(app, deps)
    job_routes.register(app, deps)
    client = TestClient(app, raise_server_exceptions=False)

    assert client.get("/api/differences", headers={"x-p": "a"}).status_code == 200
    assert client.get("/api/differences", headers={"x-p": "b"}).status_code != 200
    assert client.get("/api/jobs/nope", headers={"x-p": "b"}).status_code == 404


def _wait_for(store: JobStore, job_id: str) -> JobRecord:
    for _ in range(200):
        record = store.get(job_id)
        if record.status != "running":
            return record
        time.sleep(0.01)
    raise AssertionError("job never finished")


def test_on_finish_called_with_terminal_record(tmp_path):
    project = Project(tmp_path)
    project.init()
    store = JobStore(project)
    seen: list[JobRecord] = []

    ok = store.start("scan", lambda stream: None, on_finish=seen.append)
    _wait_for(store, ok.id)

    def fail(stream):
        raise RuntimeError("boom")

    bad = store.start("scan", fail, on_finish=seen.append)
    _wait_for(store, bad.id)
    for _ in range(200):
        if len(seen) == 2:
            break
        time.sleep(0.01)

    assert [(r.id, r.status) for r in seen] == [(ok.id, "succeeded"), (bad.id, "failed")]
    assert seen[1].error == "boom"


def test_on_finish_errors_do_not_change_the_record(tmp_path):
    project = Project(tmp_path)
    project.init()
    store = JobStore(project)

    def explode(record):
        raise RuntimeError("commit failed")

    job = store.start("scan", lambda stream: None, on_finish=explode)
    assert _wait_for(store, job.id).status == "succeeded"
