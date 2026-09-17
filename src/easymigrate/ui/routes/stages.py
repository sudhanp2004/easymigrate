"""`POST /api/scan`, `/api/plan` and `/api/apply`: background jobs for the three
long-running pipeline stages (spec §3, §4, §5).

Every pipeline callback is pre-satisfied before a job starts (spec §3) — `confirm_bucket`
and `confirm` are answered from the request body collected up front, and `ask_password`
is always `None`, so a missing password produces the engine's own clear error instead of
a server hanging on a prompt nobody can answer. No job here ever waits on a human.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable

from fastapi import Depends, FastAPI, HTTPException

from easymigrate.core import pipeline
from easymigrate.core.project import Project
from easymigrate.ir.base import StrictModel
from easymigrate.ui.jobs import JobAlreadyRunning, JobRecord, JobStore
from easymigrate.ui.security import Security

_JOB_ACTIVE_DETAIL = "a job is already running for this project"
_STALE_FINGERPRINT_DETAIL = "the plan changed since it was reviewed; run plan again"


class ScanRequest(StrictModel):
    region: str
    profile: str | None = None
    role_arn: str | None = None


class PlanRequest(StrictModel):
    gcp_project: str
    gcp_region: str | None = None
    db_passwords: dict[str, str] = {}
    create_bucket: bool = False


class ApplyRequest(StrictModel):
    plan_fingerprint: str


def plan_fingerprint(project: Project) -> str | None:
    """The hash of `plan.lock.json` the client last read (spec §5). `/api/apply` compares
    the fingerprint it is given against this, so a stale browser tab cannot approve a
    plan the user never reviewed. `plan.lock.json` already stops the engine applying a
    stale plan; this stops a stale *page* from approving one."""
    if not project.plan_lock_path.exists():
        return None
    return hashlib.sha256(project.plan_lock_path.read_bytes()).hexdigest()


def _start_or_409(
    jobs: JobStore, stage: str, work: Callable[[Callable[[str], None]], None]
) -> JobRecord:
    try:
        return jobs.start(stage, work)
    except JobAlreadyRunning as exc:
        raise HTTPException(status_code=409, detail=_JOB_ACTIVE_DETAIL) from exc


def register(app: FastAPI, project: Project, jobs: JobStore, security: Security) -> None:
    mutating = [Depends(security.require_origin), Depends(security.require_token)]

    @app.post("/api/scan", status_code=202, dependencies=mutating)
    def start_scan(body: ScanRequest) -> JobRecord:
        def work(_stream: Callable[[str], None]) -> None:
            pipeline.scan(project, region=body.region, profile=body.profile, role_arn=body.role_arn)

        return _start_or_409(jobs, "scan", work)

    @app.post("/api/plan", status_code=202, dependencies=mutating)
    def start_plan(body: PlanRequest) -> JobRecord:
        def work(_stream: Callable[[str], None]) -> None:
            pipeline.plan(
                project,
                gcp_project=body.gcp_project,
                gcp_region=body.gcp_region,
                confirm_bucket=lambda _name: body.create_bucket,
                db_passwords=body.db_passwords,
                ask_password=None,
            )

        return _start_or_409(jobs, "plan", work)

    @app.post("/api/apply", status_code=202, dependencies=mutating)
    def start_apply(body: ApplyRequest) -> JobRecord:
        current = plan_fingerprint(project)
        if current is None or current != body.plan_fingerprint:
            raise HTTPException(status_code=409, detail=_STALE_FINGERPRINT_DETAIL)

        def work(stream: Callable[[str], None]) -> None:
            pipeline.apply(project, confirm=lambda _summary: True, stream=stream)

        return _start_or_409(jobs, "apply", work)
