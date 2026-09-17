"""The FastAPI application factory: health, project state, error handlers and static
mounting (spec §5, §6). Plans 2 and 3 add routes to this same app, behind the
`Security` dependencies wired up here."""

from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from easymigrate.core.errors import EasyMigrateError
from easymigrate.core.project import Project, StageInputMissing
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


def create_app(project: Project, security: Security) -> FastAPI:
    app = FastAPI(title="EasyMigrate")

    @app.exception_handler(EasyMigrateError)
    async def _easymigrate_error_handler(_request: Request, exc: EasyMigrateError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(StageInputMissing)
    async def _stage_input_missing_handler(_request: Request, exc: StageInputMissing):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(ValidationError)
    async def _validation_error_handler(_request: Request, exc: ValidationError):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    index_html = _STATIC_DIR / "index.html"

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(request: Request, exc: StarletteHTTPException):
        # A 404 on a non-/api path is client-side routing (or, before plan 4 builds the
        # SPA, a page saying so) rather than a bare 404. /api 404s stay ordinary JSON.
        if exc.status_code == 404 and not request.url.path.startswith("/api"):
            if index_html.exists():
                return FileResponse(index_html)
            return HTMLResponse(_NOT_BUILT_HTML)
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.get("/api/health", dependencies=[Depends(security.require_origin)])
    def health() -> dict:
        return {"ok": True}

    @app.get("/api/project", dependencies=[Depends(security.require_origin)])
    def get_project():
        return project_state(project)

    if index_html.exists():
        app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="static")

    return app
