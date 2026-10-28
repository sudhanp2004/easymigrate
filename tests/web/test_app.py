"""Hosted app shell: what it refuses, how it guards, which routes exist at all."""

from __future__ import annotations

from tests.web.helpers import BASE, ORIGIN, H, make_client, start


def test_no_plan_apply_destroy_routes(settings):
    _, app = make_client(settings)
    paths = {route.path for route in app.routes}
    for forbidden in ("/api/plan", "/api/apply", "/api/destroy"):
        assert forbidden not in paths
    assert "/api/download" in paths and "/api/scan" in paths


def test_health(settings):
    client, _ = make_client(settings)
    assert client.get("/api/health").json() == {"ok": True}


def test_workspace_routes_need_cookie_401(settings):
    client, _ = make_client(settings)
    for method, path in [
        ("get", "/api/project"),
        ("get", "/api/differences"),
        ("get", "/api/aws/setup?region=us-east-1"),
        ("post", "/api/scan"),
        ("post", "/api/download"),
    ]:
        response = getattr(client, method)(path, headers=H)
        assert response.status_code == 401, (path, response.text)
        assert "Start again" in response.json()["detail"]


def test_mutating_without_header_403(settings):
    client, _ = make_client(settings)
    response = client.post("/api/workspaces", headers={"Origin": ORIGIN})
    assert response.status_code == 403
    assert response.json()["detail"] == "missing X-EasyMigrate header"


def test_cors_only_allows_origin(settings):
    client, _ = make_client(settings)
    ok = client.options(
        "/api/workspaces",
        headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-easymigrate",
        },
    )
    assert ok.headers["access-control-allow-origin"] == ORIGIN
    assert ok.headers["access-control-allow-credentials"] == "true"
    evil = client.options(
        "/api/workspaces",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-origin" not in evil.headers


def test_security_headers_present(settings):
    client, _ = make_client(settings)
    headers = client.get("/api/health").headers
    assert headers["strict-transport-security"] == "max-age=31536000; includeSubDomains"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["x-frame-options"] == "DENY"


def test_body_too_large_413(settings):
    client, _ = make_client(settings)
    start(client)
    response = client.post(
        "/api/aws/connect", headers={**H, "Content-Type": "application/json"}, content=b"x" * 70000
    )
    assert response.status_code == 413


def test_workspace_cookie_attributes(settings):
    client, _ = make_client(settings)
    cookie = start(client).headers["set-cookie"]
    for part in ("em_ws=", "HttpOnly", "Secure", "SameSite=lax", "Path=/", "Max-Age=604800"):
        assert part.lower() in cookie.lower(), part
    assert "domain" not in cookie.lower()


def test_workspace_creation_rate_limited(settings):
    client, _ = make_client(settings)
    for _ in range(10):
        start(client)
    response = client.post("/api/workspaces", headers=H)
    assert response.status_code == 429


def test_delete_workspace_clears_cookie(settings):
    client, _ = make_client(settings)
    start(client)
    response = client.delete("/api/workspace", headers=H)
    assert response.status_code == 204
    assert (
        'em_ws=""' in response.headers["set-cookie"]
        or "Max-Age=0" in response.headers["set-cookie"]
    )
    client.cookies.clear()
    assert client.get("/api/project").status_code == 401


def test_base_url_is_https(settings):
    assert BASE.startswith("https://")


def test_rate_limit_uses_cloudflare_ip_not_forwarded_for(settings):
    client, _ = make_client(settings)
    for i in range(10):
        r = client.post(
            "/api/workspaces",
            headers={**H, "CF-Connecting-IP": "203.0.113.7", "X-Forwarded-For": f"10.0.0.{i}"},
        )
        assert r.status_code == 201
    spoofed = client.post(
        "/api/workspaces",
        headers={**H, "CF-Connecting-IP": "203.0.113.7", "X-Forwarded-For": "10.9.9.9"},
    )
    assert spoofed.status_code == 429
    other = client.post("/api/workspaces", headers={**H, "CF-Connecting-IP": "198.51.100.1"})
    assert other.status_code == 201


def test_site_wide_cap_on_new_sessions(settings, monkeypatch):
    from easymigrate.web import routes

    monkeypatch.setattr(routes, "SITE_SESSIONS_PER_HOUR", 3)
    client, _ = make_client(settings)
    for i in range(3):
        assert (
            client.post("/api/workspaces", headers={**H, "CF-Connecting-IP": f"198.51.100.{i}"})
        ).status_code == 201
    busy = client.post("/api/workspaces", headers={**H, "CF-Connecting-IP": "198.51.100.99"})
    assert busy.status_code == 429
