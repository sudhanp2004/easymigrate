"""The FastAPI application factory: health, project state, error handlers and static
mounting (spec §5, §6). Plans 2 and 3 add routes to this same app, behind the
`Security` dependencies wired up here."""

from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from easymigrate.core.errors import EasyMigrateError, SchemaError
from easymigrate.core.project import Project, StageInputMissing
from easymigrate.ui.jobs import JobStore
from easymigrate.ui.routes import jobs as job_routes
from easymigrate.ui.routes import review as review_routes
from easymigrate.ui.routes import stages as stage_routes
from easymigrate.ui.routes.deps import RouteDeps
from easymigrate.ui.security import Security
from easymigrate.ui.state import project_state

_STATIC_DIR = Path(__file__).parent / "static"
_BUILD_COMMAND = "npm --prefix frontend run build"
_NOT_BUILT_HTML = (
    "<!doctype html><html><head><title>EasyMigrate UI</title></head>"
    "<body><h1>UI assets were not built</h1>"
    f"<p>Run <code>{_BUILD_COMMAND}</code>, then reload this page.</p>"
    "</body></html>"
)


def _sanitized_errors(errors) -> list[dict]:
    """Strip pydantic's `input`/`url` fields: a rejected field's submitted value (e.g. a
    database password) must never be reflected back in a response (spec §6)."""
    return [
        {
            "loc": list(error.get("loc", [])),
            "msg": error.get("msg", ""),
            "type": error.get("type", ""),
        }
        for error in errors
    ]


def create_app(project: Project, security: Security) -> FastAPI:
    # The UI is a single-purpose SPA backend, not a browsable API: the auto-registered
    # docs routes carry no origin/token dependency at all, so they were serving the full
    # route and field inventory (and, for /docs, an interactive console) to any page that
    # can reach 127.0.0.1, foreign Host included.
    app = FastAPI(title="EasyMigrate", docs_url=None, redoc_url=None, openapi_url=None)

    # A `StaticFiles(html=True)` mount at "/" (plan 4) fully matches every path for
    # every method before Starlette's router ever gets to weigh method mismatches on a
    # real route (breaking the /api 405 Allow header) or reaches the 404 handler below
    # that does the origin/Host check — for the exact "/" path (a real file,
    # `index.html`) the mount serves it directly and that check never runs at all. A
    # request-level check here closes that regardless of how routing resolves.
    @app.middleware("http")
    async def _check_origin_first(request: Request, call_next):
        try:
            security.check_origin(request.headers.get("origin"), request.headers.get("host"))
        except HTTPException as forbidden:
            return JSONResponse(
                status_code=forbidden.status_code, content={"detail": forbidden.detail}
            )
        return await call_next(request)

    # Job state lives on disk (spec §4): reap_stale runs once here, at app-factory
    # startup, so a record left "running" by a server that died mid-job is never shown
    # as live.
    jobs = JobStore(project)
    jobs.reap_stale()

    @app.exception_handler(EasyMigrateError)
    async def _easymigrate_error_handler(_request: Request, exc: EasyMigrateError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(SchemaError)
    async def _schema_error_handler(_request: Request, exc: SchemaError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(StageInputMissing)
    async def _stage_input_missing_handler(_request: Request, exc: StageInputMissing):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(ValidationError)
    async def _validation_error_handler(_request: Request, exc: ValidationError):
        errors = _sanitized_errors(exc.errors(include_url=False, include_input=False))
        return JSONResponse(status_code=422, content={"detail": errors})

    @app.exception_handler(RequestValidationError)
    async def _request_validation_error_handler(_request: Request, exc: RequestValidationError):
        return JSONResponse(status_code=422, content={"detail": _sanitized_errors(exc.errors())})

    index_html = _STATIC_DIR / "index.html"

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(request: Request, exc: StarletteHTTPException):
        # A 404 on a non-/api path is client-side routing (or, before plan 4 builds the
        # SPA, a page saying so) rather than a bare 404 — but it is still served content,
        # so it needs the same origin/Host check as every other route (this is also where
        # the now-disabled /docs, /redoc and /openapi.json paths land).
        if exc.status_code == 404 and not request.url.path.startswith("/api"):
            try:
                security.check_origin(request.headers.get("origin"), request.headers.get("host"))
            except HTTPException as forbidden:
                return JSONResponse(
                    status_code=forbidden.status_code, content={"detail": forbidden.detail}
                )
            if index_html.exists():
                return FileResponse(index_html)
            return HTMLResponse(_NOT_BUILT_HTML)
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=exc.headers,
        )

    @app.get("/api/health", dependencies=[Depends(security.require_origin)])
    def health() -> dict:
        return {"ok": True}

    @app.get("/api/project", dependencies=[Depends(security.require_origin)])
    def get_project():
        return project_state(project)

    stage_routes.register(app, project, jobs, security)
    deps = RouteDeps(
        project=lambda: project,
        jobs=lambda: jobs,
        reads=[Depends(security.require_origin)],
        mutating=[Depends(security.require_origin), Depends(security.require_token)],
    )
    job_routes.register(app, deps)
    review_routes.register(app, deps)

    # Scoped to "/assets" (Vite's hashed JS/CSS output directory), never "/": a mount at
    # "/" would fully match every /api path too (Starlette's Mount.matches ignores
    # method), silently swallowing both the origin check above the router and the
    # router's own 405-with-Allow-header logic for a wrong-method request to a real
    # route. `index.html` itself is served through the 404 handler above instead.
    assets_dir = _STATIC_DIR / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="static-assets")

    # The logo and favicons, copied verbatim from frontend/public/brand/ by Vite. A
    # second scoped mount for the same reason as "/assets": one route per icon file
    # would be the alternative, and the browser asks for these before any token is in
    # play, so they stay outside the origin-checked routes just as /assets does.
    brand_dir = _STATIC_DIR / "brand"
    if brand_dir.is_dir():
        app.mount("/brand", StaticFiles(directory=brand_dir), name="static-brand")

    return app
