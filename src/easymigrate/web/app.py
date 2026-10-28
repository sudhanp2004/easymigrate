"""The hosted app factory: `uvicorn easymigrate.web.app:create_web_app --factory`."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from easymigrate.core import registry
from easymigrate.core.errors import EasyMigrateError, SchemaError
from easymigrate.core.project import StageInputMissing
from easymigrate.ui.app import _sanitized_errors
from easymigrate.web import routes
from easymigrate.web.settings import WebSettings
from easymigrate.web.store import MemoryStore, PostgresStore, Store
from easymigrate.web.workspaces import WorkspaceManager

log = logging.getLogger("easymigrate.web")

HEADER = "X-EasyMigrate"
MAX_BODY_BYTES = 64 * 1024
CLEANUP_EVERY_S = 3600
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_SECURITY_HEADERS = {
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


def create_web_app(
    settings: WebSettings | None = None, store: Store | None = None, source: Any = None
) -> FastAPI:
    settings = settings or WebSettings.from_env()
    if store is None:
        if settings.database_url:
            store = PostgresStore(settings.database_url)
        else:
            log.warning("DATABASE_URL is not set: workspaces live in memory only")
            store = MemoryStore()
    source = source if source is not None else registry.source_for("aws")
    manager = WorkspaceManager(store, settings)
    slots = routes.ScanSlots(settings.max_concurrent_scans)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        async def cleanup_forever() -> None:
            while True:
                try:
                    removed = await asyncio.to_thread(manager.cleanup)
                    if removed:
                        log.info("deleted %d inactive workspace(s)", removed)
                except Exception:  # noqa: BLE001 - keep the loop alive; retry next hour
                    log.exception("workspace cleanup failed")
                await asyncio.sleep(CLEANUP_EVERY_S)

        task = asyncio.create_task(cleanup_forever())
        try:
            yield
        finally:
            task.cancel()

    app = FastAPI(
        title="EasyMigrate", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    app.state.manager = manager
    app.state.slots = slots

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.method not in _SAFE_METHODS:
            if request.headers.get(HEADER) != "1":
                response = JSONResponse(
                    status_code=403, content={"detail": f"missing {HEADER} header"}
                )
            elif int(request.headers.get("content-length") or 0) > MAX_BODY_BYTES:
                response = JSONResponse(status_code=413, content={"detail": "request too large"})
            else:
                response = await call_next(request)
        else:
            response = await call_next(request)
        for name, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    origins = [settings.origin]
    if settings.dev:
        origins.append("http://localhost:5173")
    # Added after `guard`, so it is the outer layer: a 403/413 from `guard` still carries
    # CORS headers and the browser shows our message instead of a network error.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Content-Type", HEADER],
        expose_headers=["Content-Disposition"],
    )

    @app.exception_handler(EasyMigrateError)
    async def _easymigrate_error(_request: Request, exc: EasyMigrateError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(SchemaError)
    async def _schema_error(_request: Request, exc: SchemaError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(StageInputMissing)
    async def _stage_input_missing(_request: Request, exc: StageInputMissing):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(ValidationError)
    async def _validation_error(_request: Request, exc: ValidationError):
        errors = _sanitized_errors(exc.errors(include_url=False, include_input=False))
        return JSONResponse(status_code=422, content={"detail": errors})

    @app.exception_handler(RequestValidationError)
    async def _request_validation_error(_request: Request, exc: RequestValidationError):
        return JSONResponse(status_code=422, content={"detail": _sanitized_errors(exc.errors())})

    routes.register(app, manager=manager, settings=settings, source=source, slots=slots)
    return app
