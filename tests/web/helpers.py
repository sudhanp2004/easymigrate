from __future__ import annotations

from fastapi.testclient import TestClient

from easymigrate.core.errors import SourceConnectionError
from easymigrate.web.app import create_web_app
from easymigrate.web.store import MemoryStore

BASE = "https://api.easymigrate.sudhan.tech"
ORIGIN = "https://easymigrate.sudhan.tech"
H = {"X-EasyMigrate": "1", "Origin": ORIGIN}
ROLE = "arn:aws:iam::123456789012:role/EasyMigrateReadOnly-abcd1234"


class FakeConnection:
    account_id = "123456789012"


class FakeSource:
    def __init__(self, error: str | None = None):
        self.error = error
        self.calls: list[dict] = []

    def connect(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise SourceConnectionError(self.error)
        return FakeConnection()

    def account_id(self, connection):
        return connection.account_id


def make_client(settings, source=None, store=None):
    app = create_web_app(settings, store=store or MemoryStore(), source=source or FakeSource())
    return TestClient(app, base_url=BASE, raise_server_exceptions=False), app


def start(client):
    response = client.post("/api/workspaces", headers=H)
    assert response.status_code == 201, response.text
    return response
