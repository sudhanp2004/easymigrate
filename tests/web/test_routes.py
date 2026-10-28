"""Hosted workspace flow: connect AWS, scan, state."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

from easymigrate.core import pipeline
from easymigrate.web import routes as web_routes
from tests.web.helpers import ROLE, FakeSource, H, make_client, start


def _connect(client, role=ROLE, region="ap-south-1"):
    return client.post("/api/aws/connect", headers=H, json={"region": region, "role_arn": role})


def test_setup_builds_quick_create_url(settings):
    client, app = make_client(settings)
    start(client)
    body = client.get("/api/aws/setup?region=eu-west-1").json()
    rec = next(iter(app.state.manager.store._records.values()))
    ws8 = rec.id[:8]
    assert body["external_id"] == rec.external_id
    assert body["stack_name"] == body["role_name"] == f"EasyMigrateReadOnly-{ws8}"
    assert body["template_url"] == settings.cfn_template_url
    assert "ap-south-1" in body["regions"] and body["regions"] == sorted(body["regions"])
    assert body["quick_create_url"] == (
        "https://console.aws.amazon.com/cloudformation/home?region=eu-west-1"
        "#/stacks/quickcreate?templateURL="
        "https%3A%2F%2Feasymigrate-public.s3.amazonaws.com%2Fdeploy%2Frole.json"
        f"&stackName=EasyMigrateReadOnly-{ws8}&param_ExternalId={rec.external_id}"
        f"&param_RoleSuffix={ws8}"
    )


def test_setup_rejects_unknown_region(settings):
    client, _ = make_client(settings)
    start(client)
    assert client.get("/api/aws/setup?region=mars-1").status_code == 400


def test_connect_trims_and_stores_account(settings):
    source = FakeSource()
    client, app = make_client(settings, source=source)
    start(client)

    response = _connect(client, role=f"  {ROLE}\n")

    assert response.status_code == 200, response.text
    assert response.json() == {"account_id": "123456789012"}
    rec = next(iter(app.state.manager.store._records.values()))
    assert source.calls == [
        {
            "region": "ap-south-1",
            "role_arn": ROLE,
            "external_id": rec.external_id,
            "session_name": f"easymigrate-{rec.id[:8]}",
        }
    ]
    state = client.get("/api/project").json()
    assert state["aws"] == {
        "region": "ap-south-1",
        "role_arn": ROLE,
        "account_id": "123456789012",
    }


def test_connect_rejects_bad_arn(settings):
    source = FakeSource()
    client, _ = make_client(settings, source=source)
    start(client)
    response = _connect(client, role="arn:aws:iam::123:user/bob")
    assert response.status_code == 400
    assert "doesn't look like a role ARN" in response.json()["detail"]
    assert source.calls == []


def test_connect_maps_assume_role_denial(settings):
    message = (
        "AWS rejected the credentials: User: arn:aws:iam::538342414628:user/easymigrate-web "
        "is not authorized to perform: sts:AssumeRole on resource: " + ROLE
    )
    client, _ = make_client(settings, source=FakeSource(error=message))
    start(client)
    detail = _connect(client).json()["detail"]
    assert detail.startswith("AWS refused access to that role.")
    assert "another session" in detail
    assert "538342414628" not in detail


def test_scan_requires_connection(settings):
    client, _ = make_client(settings)
    start(client)
    response = client.post("/api/scan", headers=H)
    assert response.status_code == 400
    assert response.json()["detail"] == "Connect AWS first."


def _fake_scan(calls):
    def scan(project, **kwargs):
        calls.append(kwargs)
        project.write_text(project.dir / "scanned.txt", "yes")

    return scan


def _wait_job(client, job_id):
    for _ in range(300):
        record = client.get(f"/api/jobs/{job_id}").json()
        if record["status"] != "running":
            return record
        time.sleep(0.01)
    raise AssertionError("job did not finish")


def test_scan_passes_external_id_and_commits(settings, monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(pipeline, "scan", _fake_scan(calls))
    source = FakeSource()
    client, app = make_client(settings, source=source)
    start(client)
    _connect(client)

    job = client.post("/api/scan", headers=H)
    assert job.status_code == 202, job.text
    assert _wait_job(client, job.json()["id"])["status"] == "succeeded"

    rec = next(iter(app.state.manager.store._records.values()))
    assert calls[0]["region"] == "ap-south-1"
    assert calls[0]["role_arn"] == ROLE
    assert calls[0]["external_id"] == rec.external_id
    assert calls[0]["session_name"] == f"easymigrate-{rec.id[:8]}"
    assert calls[0]["source"] is source
    for _ in range(100):  # on_finish runs just after the record is written
        if app.state.slots.active() == 0:
            break
        time.sleep(0.01)
    archive = app.state.manager.store.load_archive(rec.id)
    assert archive is not None
    assert app.state.slots.active() == 0


def test_scan_busy_when_slots_full(settings, monkeypatch):
    monkeypatch.setattr(pipeline, "scan", _fake_scan([]))
    client, app = make_client(settings)
    start(client)
    _connect(client)
    app.state.slots.acquire("other-1")
    app.state.slots.acquire("other-2")

    response = client.post("/api/scan", headers=H)

    assert response.status_code == 503
    assert response.json()["detail"] == (
        "EasyMigrate is busy with other scans. Try again in a minute."
    )


def test_stale_slot_freed_after_20_min():
    slots = web_routes.ScanSlots(1)
    t0 = datetime(2026, 10, 28, tzinfo=UTC)
    assert slots.acquire("a", now=t0)
    assert not slots.acquire("b", now=t0 + timedelta(minutes=19))
    assert slots.acquire("b", now=t0 + timedelta(minutes=21))


def test_scan_daily_limit(settings, monkeypatch):
    monkeypatch.setattr(pipeline, "scan", _fake_scan([]))
    client, _ = make_client(settings)
    start(client)
    _connect(client)
    for _ in range(10):
        job = client.post("/api/scan", headers=H)
        assert job.status_code == 202, job.text
        _wait_job(client, job.json()["id"])
    assert client.post("/api/scan", headers=H).status_code == 429


def test_project_state_shape(settings):
    client, _ = make_client(settings)
    start(client)
    state = client.get("/api/project").json()
    assert [s["name"] for s in state["stages"]] == ["scan", "analyze", "resolve"]
    assert state["root"] == "workspace"
    assert state["aws"] is None
    assert state["active_job"] is None
    assert "expires_at" in state and "suggested_gcp_region" in state
