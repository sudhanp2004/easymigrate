"""The FastAPI application factory: health, project state, error handling and static
mounting with the "not built" fallback (plan 1 task 3)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

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
