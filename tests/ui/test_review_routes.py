"""GET /api/differences, POST /api/analyze, GET/PUT /api/decisions, POST /api/resolve
and GET /api/report — the HTTP surface for the review step (spec §5, plan 3 tasks 2-4).

Project fixtures are built by copying a real scan snapshot from
`tests/plugins/database/fixtures/` into `.easymigrate/snapshot/aws` and running the
real pipeline (`analyze`, `resolve`) over it, so the differences served here are real
ones, not hand-built approximations.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from easymigrate.core.pipeline import analyze as run_analyze
from easymigrate.core.pipeline import plan as run_plan
from easymigrate.core.pipeline import resolve as run_resolve
from easymigrate.core.plugin import SCAN_FILE
from easymigrate.core.project import Project, SourceConfig
from easymigrate.differences.decisions import from_yaml
from easymigrate.ui.app import create_app
from easymigrate.ui.security import Security

PORT = 54799
TOKEN = "review-test-token"
HEADERS = {"Host": f"127.0.0.1:{PORT}", "X-EasyMigrate-Token": TOKEN}

DB_FIXTURES = Path(__file__).parent.parent / "plugins" / "database" / "fixtures"


class FakeGcp:
    def credentials_ok(self) -> bool:
        return True

    def ensure_bucket(self, gcp_project, name, region, confirm):
        return True

    def services_enabled(self, gcp_project, required):
        return None


class FakeTofu:
    def init(self, backend_config=None, *, backend=True):
        pass

    def validate(self):
        pass

    def plan(self, out: Path, stream=None) -> str:
        out.write_bytes(b"fake-plan-bytes")
        text = "Plan: 1 to add, 0 to change, 0 to destroy."
        if stream is not None:
            stream(text)
        return text

    def show_json(self, plan: Path) -> dict:
        return {"resource_changes": []}


def _scan_from_fixture(project: Project, fixture: str) -> None:
    project.init()
    snapshot_dir = project.snapshot_dir("aws")
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    for source in (DB_FIXTURES / fixture / "snapshot").glob("*.json"):
        shutil.copy(source, snapshot_dir / source.name)
    project.write_json(
        snapshot_dir / SCAN_FILE,
        {
            "account_id": "123456789012",
            "region": "ap-south-1",
            "scanned_at": "2024-01-01T00:00:00+00:00",
            "findings": [],
        },
    )
    config = project.load_config()
    project.save_config(
        config.model_copy(
            update={
                "source": SourceConfig(cloud="aws", account_id="123456789012", region="ap-south-1")
            }
        )
    )


def _analyzed(project: Project, fixture: str) -> None:
    _scan_from_fixture(project, fixture)
    run_analyze(project)


@pytest.fixture
def project(tmp_path: Path) -> Project:
    return Project(tmp_path)


@pytest.fixture
def security() -> Security:
    return Security(token=TOKEN, host="127.0.0.1", port=PORT)


@pytest.fixture
def client(project: Project, security: Security) -> TestClient:
    return TestClient(create_app(project, security))


# --- GET /api/differences -------------------------------------------------------------


def test_differences_before_analyze_is_400_with_engine_message(client: TestClient):
    resp = client.get("/api/differences", headers=HEADERS)
    assert resp.status_code == 400
    assert "analyze" in resp.json()["detail"]


def test_differences_after_analyze_lists_every_key_sorted_with_a_strategy(
    client: TestClient, project: Project
):
    _analyzed(project, "old_engine_version")
    resp = client.get("/api/differences", headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    keys = [d["key"] for d in body]
    assert keys == sorted(keys)
    assert len(keys) > 0
    for d in body:
        assert len(d["strategies"]) >= 1

    engine_diff = next(d for d in body if d["rule_id"] == "DB-ENGINE-VERSION")
    ids = {s["id"] for s in engine_diff["strategies"]}
    assert ids == {"upgrade_to_oldest_supported", "manual"}


# --- POST /api/analyze -----------------------------------------------------------------


def test_analyze_runs_pipeline_and_returns_counts_and_findings(
    client: TestClient, project: Project
):
    _scan_from_fixture(project, "old_engine_version")
    resp = client.post("/api/analyze", headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["counts"]["database_instance"] == 1
    assert "findings" in body
    assert project.differences_path.exists()


# --- GET/PUT /api/decisions --------------------------------------------------------------


def test_put_valid_strategy_persists_and_round_trips_through_from_yaml(
    client: TestClient, project: Project
):
    _analyzed(project, "old_engine_version")
    key = "DB-TIER@database_instance:legacy-db"

    resp = client.put(
        f"/api/decisions/{key}",
        json={"strategy": "nearest_custom_tier"},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["strategy"] == "nearest_custom_tier"
    assert body["decided_by"] == "user"

    decisions = from_yaml(project.decisions_path.read_text(encoding="utf-8"))
    decision = next(d for d in decisions.decisions if d.key == key)
    assert decision.strategy == "nearest_custom_tier"
    assert decision.decided_by == "user"


def test_put_disallowed_strategy_is_400_listing_allowed_strategies(
    client: TestClient, project: Project
):
    _analyzed(project, "old_engine_version")
    key = "DB-ENGINE-VERSION@database_instance:legacy-db"

    resp = client.put(
        f"/api/decisions/{key}",
        json={"strategy": "nearest_major"},
        headers=HEADERS,
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "upgrade_to_oldest_supported" in detail
    assert "manual" in detail


def test_put_unknown_key_is_404(client: TestClient, project: Project):
    _analyzed(project, "old_engine_version")
    resp = client.put(
        "/api/decisions/DB-NOPE@database_instance:legacy-db",
        json={"strategy": "manual"},
        headers=HEADERS,
    )
    assert resp.status_code == 404


def test_put_private_ip_overlapping_vpc_is_400_naming_the_overlapping_vpc(
    client: TestClient, project: Project
):
    _analyzed(project, "psa_range_conflict")
    key = "DB-PRIVATE-IP@database_instance:conflict-db"

    resp = client.put(
        f"/api/decisions/{key}",
        json={
            "strategy": "psa_auto_range",
            "params": {"range": "10.64.0.0", "prefix_length": 16},
        },
        headers=HEADERS,
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "psa-conflict-vpc" in detail
    assert "10.64.0.0/16" in detail


def test_put_private_ip_valid_range_persists_with_those_params(
    client: TestClient, project: Project
):
    _analyzed(project, "psa_range_conflict")
    key = "DB-PRIVATE-IP@database_instance:conflict-db"

    resp = client.put(
        f"/api/decisions/{key}",
        json={
            "strategy": "psa_auto_range",
            "params": {"range": "10.200.0.0", "prefix_length": 16},
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["params"] == {"range": "10.200.0.0", "prefix_length": 16}


def test_switching_strategy_refills_params_from_new_strategys_defaults(
    client: TestClient, project: Project
):
    _analyzed(project, "psa_range_conflict")
    key = "DB-PRIVATE-IP@database_instance:conflict-db"

    first = client.put(
        f"/api/decisions/{key}",
        json={
            "strategy": "psa_auto_range",
            "params": {"range": "10.200.0.0", "prefix_length": 16},
        },
        headers=HEADERS,
    )
    assert first.json()["params"] == {"range": "10.200.0.0", "prefix_length": 16}

    switched = client.put(
        f"/api/decisions/{key}",
        json={"strategy": "public_ip_authorized_networks"},
        headers=HEADERS,
    )
    assert switched.status_code == 200
    assert switched.json()["params"] == {}


def test_manual_strategy_without_ack_persists_and_blocks(client: TestClient, project: Project):
    _analyzed(project, "old_engine_version")
    key = "DB-PRIVATE-IP@database_instance:legacy-db"

    resp = client.put(
        f"/api/decisions/{key}",
        json={"strategy": "manual"},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["needs_acknowledgement"] is True

    decisions_resp = client.get("/api/decisions", headers=HEADERS)
    assert decisions_resp.status_code == 200
    blocked = decisions_resp.json()["blocked"]
    assert any(key in b for b in blocked)


def test_acknowledging_manual_strategy_clears_the_block(client: TestClient, project: Project):
    _analyzed(project, "old_engine_version")
    key = "DB-PRIVATE-IP@database_instance:legacy-db"

    client.put(f"/api/decisions/{key}", json={"strategy": "manual"}, headers=HEADERS)
    ack = client.put(
        f"/api/decisions/{key}",
        json={"strategy": "manual", "acknowledged": True},
        headers=HEADERS,
    )
    assert ack.status_code == 200
    assert ack.json()["needs_acknowledgement"] is False

    decisions_resp = client.get("/api/decisions", headers=HEADERS)
    blocked = decisions_resp.json()["blocked"]
    assert not any(key in b for b in blocked)


def test_put_omitting_acknowledged_preserves_an_existing_acknowledgement(
    client: TestClient, project: Project
):
    """spec §5 calls acknowledgement "optional": omitting it on a later edit (e.g. a
    params-only change) must not silently revoke an acknowledgement already given."""
    _analyzed(project, "old_engine_version")
    key = "DB-PRIVATE-IP@database_instance:legacy-db"

    client.put(f"/api/decisions/{key}", json={"strategy": "manual"}, headers=HEADERS)
    client.put(
        f"/api/decisions/{key}",
        json={"strategy": "manual", "acknowledged": True},
        headers=HEADERS,
    )

    resp = client.put(
        f"/api/decisions/{key}",
        json={"strategy": "manual"},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["needs_acknowledgement"] is False

    decisions_resp = client.get("/api/decisions", headers=HEADERS)
    blocked = decisions_resp.json()["blocked"]
    assert not any(key in b for b in blocked)


# --- POST /api/resolve ------------------------------------------------------------------


def test_resolve_returns_same_shape_as_get_decisions(client: TestClient, project: Project):
    _analyzed(project, "old_engine_version")
    resp = client.post("/api/resolve", headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert "decisions" in body
    assert "blocked" in body
    assert len(body["decisions"]) > 0
    assert project.decisions_path.exists()


# --- GET /api/report ---------------------------------------------------------------------


def test_report_returns_database_section_facts(client: TestClient, project: Project, monkeypatch):
    _analyzed(project, "old_engine_version")
    run_resolve(project, accept_defaults=True)
    run_plan(
        project,
        gcp_project="my-proj",
        confirm_bucket=lambda _name: True,
        db_passwords={"database_instance:legacy-db": "sentinel-pw-123"},
        gcp=FakeGcp(),
        tofu=FakeTofu(),
    )

    resp = client.get("/api/report", headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    databases = body["databases"]
    assert len(databases) == 1
    assert databases[0]["id"] == "database_instance:legacy-db"

    assert "sentinel-pw-123" not in resp.text
