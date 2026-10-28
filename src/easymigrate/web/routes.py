"""Hosted routes: workspace, AWS connection, scan, download, plus the shared review and
job routes of the local UI, scoped to the visitor's workspace.

No `from __future__ import annotations` here: FastAPI must evaluate the dependency aliases
defined inside `register` (they close over the workspace manager)."""

import re
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Request, Response

from easymigrate.core import pipeline
from easymigrate.core.errors import EasyMigrateError, SourceConnectionError
from easymigrate.core.project import Project
from easymigrate.core.regions import REGION_MAP
from easymigrate.ir.base import StrictModel
from easymigrate.ui.jobs import JobAlreadyRunning, JobRecord, JobStore
from easymigrate.ui.routes import jobs as job_routes
from easymigrate.ui.routes import review as review_routes
from easymigrate.ui.routes.deps import RouteDeps
from easymigrate.ui.state import ProjectState, project_state
from easymigrate.web.download import build_zip
from easymigrate.web.settings import WebSettings
from easymigrate.web.store import WorkspaceRecord
from easymigrate.web.workspaces import RETENTION, WorkspaceManager

COOKIE = "em_ws"
COOKIE_MAX_AGE = 7 * 24 * 3600
SESSION_ENDED = (
    "Your session has ended. Data is deleted after 7 days without activity. Start again."
)
ROLE_ARN = re.compile(r"^arn:aws:iam::\d{12}:role/[\w+=,.@/-]{1,128}$")
GCP_PROJECT = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
GCP_REGION = re.compile(r"^[a-z]+-[a-z]+\d{1,2}$")
DEFAULT_GCP_PROJECT = "my-gcp-project"
SLOT_TTL = timedelta(minutes=20)
_WEB_STAGES = ("scan", "analyze", "resolve")


class ScanSlots:
    """At most `limit` scans at once on this instance. A slot older than 20 minutes counts
    as free: a thread stuck in an AWS call cannot be killed, but it must not block everyone."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._held: dict[str, datetime] = {}
        self._lock = threading.Lock()

    def acquire(self, key: str, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        with self._lock:
            self._held = {k: t for k, t in self._held.items() if now - t < SLOT_TTL}
            if key not in self._held and len(self._held) >= self.limit:
                return False
            self._held[key] = now
            return True

    def release(self, key: str) -> None:
        with self._lock:
            self._held.pop(key, None)

    def active(self) -> int:
        with self._lock:
            return len(self._held)


class AwsInfo(StrictModel):
    region: str | None
    role_arn: str | None
    account_id: str | None


class WebState(ProjectState):
    aws: AwsInfo | None = None
    suggested_gcp_region: str | None = None
    expires_at: datetime


class ConnectRequest(StrictModel):
    region: str
    role_arn: str


class DownloadRequest(StrictModel):
    gcp_project: str = ""
    gcp_region: str | None = None


def _set_cookie(response: Response, secret: str, settings: WebSettings) -> None:
    response.set_cookie(
        COOKIE,
        secret,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        secure=not settings.dev,
        samesite="lax",
        path="/",
    )


def _connect_error(message: str) -> str:
    if "AssumeRole" in message:
        return (
            "AWS refused access to that role. Check the stack shows CREATE_COMPLETE and that "
            "it was created from this browser's link — a stack made in another session has a "
            "different external ID."
        )
    return message


def register(
    app: FastAPI,
    *,
    manager: WorkspaceManager,
    settings: WebSettings,
    source: Any,
    slots: ScanSlots,
) -> None:
    job_stores: dict[str, JobStore] = {}
    job_stores_lock = threading.Lock()

    def workspace(request: Request) -> WorkspaceRecord:
        rec = manager.authenticate(request.cookies.get(COOKIE))
        if rec is None:
            raise HTTPException(status_code=401, detail=SESSION_ENDED)
        return rec

    Ws = Annotated[WorkspaceRecord, Depends(workspace)]

    def project(request: Request, rec: Ws):
        current = manager.project_for(rec)
        yield current
        if request.method not in ("GET", "HEAD"):
            manager.commit(rec)

    Proj = Annotated[Project, Depends(project)]

    def jobs(rec: Ws, current: Proj):
        with job_stores_lock:
            store = job_stores.get(rec.id)
            if store is None or store.project.root != current.root:
                store = job_stores[rec.id] = JobStore(current)
            return store

    deps = RouteDeps(project=project, jobs=jobs, reads=[], mutating=[])
    review_routes.register(app, deps)
    job_routes.register(app, deps)

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True}

    @app.post("/api/workspaces", status_code=201)
    def create_workspace(request: Request, response: Response) -> dict:
        ip = request.client.host if request.client else "unknown"
        if not manager.store.hit(f"ws-create:{ip}", 3600, 10):
            raise HTTPException(
                status_code=429,
                detail="Too many new sessions from your network. Try again in an hour.",
            )
        rec, secret = manager.create()
        _set_cookie(response, secret, settings)
        return {"ws8": rec.ws8}

    @app.delete("/api/workspace", status_code=204)
    def delete_workspace(rec: Ws) -> Response:
        manager.delete(rec)
        with job_stores_lock:
            job_stores.pop(rec.id, None)
        response = Response(status_code=204)
        response.delete_cookie(
            COOKIE, path="/", secure=not settings.dev, httponly=True, samesite="lax"
        )
        return response

    @app.get("/api/project")
    def get_project(
        request: Request,
        response: Response,
        rec: Ws,
        current: Proj,
    ) -> WebState:
        base = project_state(current)
        _set_cookie(response, request.cookies[COOKIE], settings)  # rolling 7 days
        aws = (
            AwsInfo(region=rec.region, role_arn=rec.role_arn, account_id=rec.account_id)
            if rec.role_arn
            else None
        )
        region = rec.region or (base.source or {}).get("region")
        return WebState(
            root="workspace",
            stages=[s for s in base.stages if s.name in _WEB_STAGES],
            source=base.source,
            target=None,
            blocked=base.blocked,
            active_job=base.active_job,
            aws=aws,
            suggested_gcp_region=REGION_MAP.get(region) if region else None,
            expires_at=rec.last_active_at + RETENTION,
        )

    @app.get("/api/aws/setup")
    def aws_setup(region: str, rec: Ws) -> dict:
        if region not in REGION_MAP:
            raise HTTPException(status_code=400, detail="Pick one of the listed AWS regions.")
        name = f"EasyMigrateReadOnly-{rec.ws8}"
        url = (
            f"https://console.aws.amazon.com/cloudformation/home?region={region}"
            f"#/stacks/quickcreate?templateURL={quote(settings.cfn_template_url, safe='')}"
            f"&stackName={name}&param_ExternalId={rec.external_id}&param_RoleSuffix={rec.ws8}"
        )
        return {
            "external_id": rec.external_id,
            "stack_name": name,
            "role_name": name,
            "template_url": settings.cfn_template_url,
            "regions": sorted(REGION_MAP),
            "quick_create_url": url,
        }

    @app.post("/api/aws/connect")
    def aws_connect(body: ConnectRequest, rec: Ws) -> dict:
        region = body.region.strip()
        role_arn = body.role_arn.strip()
        if region not in REGION_MAP:
            raise HTTPException(status_code=400, detail="Pick one of the listed AWS regions.")
        if not ROLE_ARN.match(role_arn):
            raise HTTPException(
                status_code=400,
                detail=(
                    "That doesn't look like a role ARN. Copy the RoleArn value from the stack's "
                    "Outputs tab; it starts with arn:aws:iam::"
                ),
            )
        try:
            connection = source.connect(
                region=region,
                role_arn=role_arn,
                external_id=rec.external_id,
                session_name=f"easymigrate-{rec.ws8}",
            )
        except SourceConnectionError as exc:
            raise HTTPException(status_code=400, detail=_connect_error(str(exc))) from None
        account_id = source.account_id(connection)
        rec.region, rec.role_arn, rec.account_id = region, role_arn, account_id
        manager.store.update(rec)
        return {"account_id": account_id}

    @app.post("/api/scan", status_code=202)
    def start_scan(
        rec: Ws,
        current: Proj,
        store: Annotated[JobStore, Depends(jobs)],
    ) -> JobRecord:
        if not rec.role_arn or not rec.region:
            raise HTTPException(status_code=400, detail="Connect AWS first.")
        if store.active() is not None:
            raise HTTPException(status_code=409, detail="A scan is already running.")
        if not slots.acquire(rec.id):
            raise HTTPException(
                status_code=503,
                detail="EasyMigrate is busy with other scans. Try again in a minute.",
            )
        if not manager.store.hit(f"scan:{rec.id}", 86400, 10):
            slots.release(rec.id)
            raise HTTPException(
                status_code=429,
                detail="You've reached today's limit of 10 scans. Try again tomorrow.",
            )
        region, role_arn = rec.region, rec.role_arn

        def work(_stream: Callable[[str], None]) -> None:
            pipeline.scan(
                current,
                region=region,
                role_arn=role_arn,
                external_id=rec.external_id,
                session_name=f"easymigrate-{rec.ws8}",
                source=source,
            )

        def finished(_record: JobRecord) -> None:
            slots.release(rec.id)
            manager.commit(rec)

        try:
            return store.start("scan", work, on_finish=finished)
        except JobAlreadyRunning:
            slots.release(rec.id)
            raise HTTPException(status_code=409, detail="A scan is already running.") from None

    @app.post("/api/download")
    def download(
        body: DownloadRequest,
        rec: Ws,
        current: Proj,
    ) -> Response:
        gcp_project = body.gcp_project.strip() or DEFAULT_GCP_PROJECT
        if not GCP_PROJECT.match(gcp_project):
            raise HTTPException(
                status_code=400,
                detail=(
                    "That isn't a valid Google Cloud project ID: 6–30 lowercase letters, digits "
                    "or hyphens, starting with a letter. Leave it empty to fill it in later."
                ),
            )
        gcp_region = (body.gcp_region or "").strip() or None
        if gcp_region is not None and not GCP_REGION.match(gcp_region):
            raise HTTPException(status_code=400, detail="That isn't a Google Cloud region name.")
        try:
            summary = pipeline.generate(current, gcp_project=gcp_project, gcp_region=gcp_region)
        except EasyMigrateError as exc:
            if "no GCP region mapping" in str(exc):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "EasyMigrate doesn't know which Google Cloud region matches this AWS "
                        "region. Pick a Google Cloud region and download again."
                    ),
                ) from None
            raise
        source_config = current.load_config().source
        account = rec.account_id or (source_config.account_id if source_config else "aws")
        region = source_config.region if source_config else (rec.region or "region")
        return Response(
            content=build_zip(current, summary),
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="easymigrate-{account}-{region}.zip"'
            },
        )
