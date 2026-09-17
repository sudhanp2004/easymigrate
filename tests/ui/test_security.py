"""Security is the point of plan 1: loopback-only origin/host checks and a launch token
that a cross-site request can never read (spec §6)."""

from __future__ import annotations

import secrets

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from easymigrate.ui.security import TOKEN_HEADER, Security, new_token

PORT = 54732
TOKEN = "test-token-" + secrets.token_hex(8)


def _make_app(security: Security) -> FastAPI:
    app = FastAPI()

    @app.get("/api/project", dependencies=[Depends(security.require_origin)])
    def get_project():
        return {"ok": True}

    @app.post(
        "/api/apply",
        dependencies=[Depends(security.require_origin), Depends(security.require_token)],
    )
    def post_apply():
        return {"ok": True}

    return app


@pytest.fixture
def security() -> Security:
    return Security(token=TOKEN, host="127.0.0.1", port=PORT)


@pytest.fixture
def client(security: Security) -> TestClient:
    return TestClient(_make_app(security))


def test_new_token_is_urlsafe_and_long():
    token = new_token()
    assert isinstance(token, str)
    assert len(token) >= 32


def test_new_token_is_random():
    assert new_token() != new_token()


def test_mutating_request_with_no_token_is_rejected(client: TestClient):
    resp = client.post(
        "/api/apply", headers={"Origin": f"http://127.0.0.1:{PORT}", "Host": f"127.0.0.1:{PORT}"}
    )
    assert resp.status_code == 403


def test_mutating_request_with_wrong_token_is_rejected(client: TestClient):
    resp = client.post(
        "/api/apply",
        headers={
            "Origin": f"http://127.0.0.1:{PORT}",
            "Host": f"127.0.0.1:{PORT}",
            TOKEN_HEADER: "not-the-right-token",
        },
    )
    assert resp.status_code == 403


def test_mutating_request_with_right_token_passes(client: TestClient):
    resp = client.post(
        "/api/apply",
        headers={
            "Origin": f"http://127.0.0.1:{PORT}",
            "Host": f"127.0.0.1:{PORT}",
            TOKEN_HEADER: TOKEN,
        },
    )
    assert resp.status_code == 200


def test_get_with_foreign_origin_is_rejected(client: TestClient):
    resp = client.get(
        "/api/project",
        headers={"Origin": "https://evil.example", "Host": f"127.0.0.1:{PORT}"},
    )
    assert resp.status_code == 403


def test_foreign_host_header_is_rejected_dns_rebinding(client: TestClient):
    """Host is what closes DNS rebinding: the browser sends the attacker's hostname
    even though the connection is really to 127.0.0.1."""
    resp = client.get(
        "/api/project",
        headers={"Host": "evil.example"},
    )
    assert resp.status_code == 403


def test_no_origin_with_correct_host_is_allowed(client: TestClient):
    """curl and other same-origin, non-CORS requests send no Origin at all."""
    resp = client.get("/api/project", headers={"Host": f"127.0.0.1:{PORT}"})
    assert resp.status_code == 200


def test_localhost_origin_and_host_are_also_accepted(client: TestClient):
    resp = client.get(
        "/api/project",
        headers={"Origin": f"http://localhost:{PORT}", "Host": f"localhost:{PORT}"},
    )
    assert resp.status_code == 200


def test_check_token_uses_constant_time_comparison(monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple[str, str]] = []
    real_compare = secrets.compare_digest

    def _spy(a, b):
        calls.append((a, b))
        return real_compare(a, b)

    monkeypatch.setattr(secrets, "compare_digest", _spy)
    security = Security(token=TOKEN, host="127.0.0.1", port=PORT)
    security.check_token(TOKEN)
    assert calls, "check_token must compare with secrets.compare_digest"


def test_check_token_rejects_empty_string(security: Security):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        security.check_token("")
    assert exc_info.value.status_code == 403


def test_check_token_rejects_none(security: Security):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        security.check_token(None)
    assert exc_info.value.status_code == 403


def test_token_never_leaks_in_any_response_body(client: TestClient):
    """A guard test: the launch token, even when correctly supplied, must never be
    echoed back in a response body."""
    responses = [
        client.get("/api/project", headers={"Host": f"127.0.0.1:{PORT}"}),
        client.post(
            "/api/apply",
            headers={
                "Origin": f"http://127.0.0.1:{PORT}",
                "Host": f"127.0.0.1:{PORT}",
                TOKEN_HEADER: TOKEN,
            },
        ),
        client.post(
            "/api/apply",
            headers={
                "Origin": f"http://127.0.0.1:{PORT}",
                "Host": f"127.0.0.1:{PORT}",
                TOKEN_HEADER: "wrong",
            },
        ),
    ]
    for resp in responses:
        assert TOKEN not in resp.text
