"""The FastAPI application factory: health, project state, error handling and static
mounting with the "not built" fallback (plan 1 task 3)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import BaseModel

from easymigrate.core.project import Project, StageInputMissing
from easymigrate.ui.app import create_app
from easymigrate.ui.security import Security

PORT = 54733
TOKEN = "app-test-token"
HEADERS = {"Host": f"127.0.0.1:{PORT}"}


@pytest.fixture
def project(tmp_path: Path) -> Project:
    return Project(tmp_path)


@pytest.fixture
def security() -> Security:
    return Security(token=TOKEN, host="127.0.0.1", port=PORT)


@pytest.fixture
def client(project: Project, security: Security) -> TestClient:
    app = create_app(project, security)
    return TestClient(app)


def test_health_returns_ok(client: TestClient):
    resp = client.get("/api/health", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_project_returns_state_shape(client: TestClient, project: Project):
    resp = client.get("/api/project", headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["root"] == str(project.root)
    assert {s["name"] for s in body["stages"]} == {
        "scan",
        "analyze",
        "resolve",
        "plan",
        "apply",
    }
    assert body["source"] is None
    assert body["target"] is None
    assert body["blocked"] == []


def test_project_rejects_foreign_origin(client: TestClient):
    resp = client.get("/api/project", headers={**HEADERS, "Origin": "https://evil.example"})
    assert resp.status_code == 403


def test_health_rejects_foreign_origin(client: TestClient):
    resp = client.get("/api/health", headers={**HEADERS, "Origin": "https://evil.example"})
    assert resp.status_code == 403


def test_stage_input_missing_maps_to_400_with_verbatim_message(
    project: Project, security: Security
):
    app = create_app(project, security)
    error = StageInputMissing(project.ir_path, "analyze")

    @app.get("/api/_boom_stage_input_missing")
    def _boom():
        raise error

    client = TestClient(app)
    resp = client.get("/api/_boom_stage_input_missing", headers=HEADERS)
    assert resp.status_code == 400
    assert resp.json()["detail"] == str(error)


def test_easymigrate_error_maps_to_400_with_verbatim_message(project: Project, security: Security):
    from easymigrate.core.errors import PlanStale

    app = create_app(project, security)
    error = PlanStale(
        "the plan is stale (files changed since `plan`); run `easymigrate plan` again"
    )

    @app.get("/api/_boom_plan_stale")
    def _boom():
        raise error

    client = TestClient(app)
    resp = client.get("/api/_boom_plan_stale", headers=HEADERS)
    assert resp.status_code == 400
    assert resp.json()["detail"] == str(error)


def test_docs_routes_are_disabled(project: Project, security: Security):
    """FastAPI's auto-registered /docs, /redoc and /openapi.json carry no origin/token
    dependency at all; the UI is a single-purpose SPA backend and has no use for them."""
    app = create_app(project, security)
    assert app.docs_url is None
    assert app.redoc_url is None
    assert app.openapi_url is None


def test_openapi_json_does_not_serve_the_schema(client: TestClient):
    resp = client.get("/openapi.json", headers=HEADERS)
    assert '"paths"' not in resp.text


def test_foreign_host_is_rejected_on_a_non_api_path(client: TestClient):
    """The origin/Host check must apply to every route, not only /api/*. This is what
    closed DNS rebinding against /openapi.json, /docs and the SPA root."""
    resp = client.get("/openapi.json", headers={"Host": "evil.example"})
    assert resp.status_code == 403


def test_foreign_host_is_rejected_on_the_spa_root(client: TestClient):
    resp = client.get("/", headers={"Host": "evil.example"})
    assert resp.status_code == 403


class _SecretBody(BaseModel):
    secret: str


def test_request_validation_error_does_not_leak_submitted_value(
    project: Project, security: Security
):
    app = create_app(project, security)

    @app.post("/api/_boom_request_validation")
    def _boom(body: _SecretBody):
        return {"ok": True}

    client = TestClient(app)
    sentinel = "SENTINEL-PASSWORD-VALUE"
    resp = client.post(
        "/api/_boom_request_validation",
        json={"secret": {"nested": sentinel}},
        headers=HEADERS,
    )
    assert resp.status_code == 422
    assert sentinel not in resp.text


def test_pydantic_validation_error_does_not_leak_submitted_value(
    project: Project, security: Security
):
    app = create_app(project, security)
    sentinel = "SENTINEL-PASSWORD-VALUE"

    @app.get("/api/_boom_pydantic_validation")
    def _boom():
        _SecretBody.model_validate({"secret": {"nested": sentinel}})

    client = TestClient(app)
    resp = client.get("/api/_boom_pydantic_validation", headers=HEADERS)
    assert resp.status_code == 422
    assert sentinel not in resp.text


def test_every_route_has_origin_dependency_and_mutations_have_token(
    project: Project, security: Security
):
    """Structural guard: plans 2 and 3 add roughly ten more routes behind these same
    dependencies. This walks the *real* app rather than a hand-built fixture, so a route
    added later without the right dependency fails here instead of shipping unguarded."""
    app = create_app(project, security)
    api_routes = [route for route in app.routes if isinstance(route, APIRoute)]
    assert api_routes, "expected at least /api/health and /api/project"

    for route in api_routes:
        dependency_calls = {dep.call for dep in route.dependant.dependencies}
        assert security.require_origin in dependency_calls, (
            f"{route.path} is missing the origin dependency"
        )
        mutating_methods = route.methods - {"HEAD", "OPTIONS", "GET"}
        if mutating_methods:
            assert security.require_token in dependency_calls, (
                f"{route.path} ({mutating_methods}) is missing the token dependency"
            )


def test_method_not_allowed_keeps_the_allow_header(client: TestClient):
    """The blanket StarletteHTTPException handler used to drop exc.headers, silently
    stripping the Allow header from a 405 (and, later, Retry-After from a 409)."""
    resp = client.post("/api/health", headers=HEADERS)
    assert resp.status_code == 405
    assert "Allow" in resp.headers
    assert "GET" in resp.headers["Allow"]


def test_unknown_path_with_no_built_assets_is_not_a_bare_404(client: TestClient):
    resp = client.get("/", headers=HEADERS)
    assert resp.status_code != 404
    assert "npm --prefix frontend run build" in resp.text


def test_unknown_non_api_path_is_also_not_a_bare_404(client: TestClient):
    resp = client.get("/some/client/route", headers=HEADERS)
    assert resp.status_code != 404
    assert "npm --prefix frontend run build" in resp.text


def test_cli_does_not_import_ui_at_module_scope():
    cli_path = Path(__file__).resolve().parents[2] / "src" / "easymigrate" / "cli.py"
    tree = ast.parse(cli_path.read_text(encoding="utf-8"))
    module_level_imports: set[str] = set()
    for node in tree.body:  # only top-level statements, not inside functions
        if isinstance(node, ast.Import):
            module_level_imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            module_level_imports.add(node.module)
    assert not any(name.startswith("easymigrate.ui") for name in module_level_imports)
