"""Service Usage API check tests (plan 4, `plan` preflight §6.4), using a fake session
so nothing touches real GCP."""

from __future__ import annotations

import pytest

from easymigrate.providers.gcp import services


class _FakeResponse:
    def __init__(self, status_code: int, data: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._data = data or {}
        self.text = text

    def json(self):
        return self._data


class _FakeSession:
    def __init__(self, pages: list[dict], status_code: int = 200):
        self._pages = pages
        self._status_code = status_code
        self.calls: list[dict] = []

    def get(self, url, params=None):
        self.calls.append(dict(params or {}))
        index = len(self.calls) - 1
        page = self._pages[index]
        return _FakeResponse(self._status_code, page)


def test_enabled_services_filters_to_enabled_state():
    session = _FakeSession(
        [
            {
                "services": [
                    {"config": {"name": "sqladmin.googleapis.com"}, "state": "ENABLED"},
                ]
            }
        ]
    )

    assert services.enabled_services("my-proj", session=session) == {"sqladmin.googleapis.com"}


def test_enabled_services_paginates():
    session = _FakeSession(
        [
            {
                "services": [{"config": {"name": "a.googleapis.com"}, "state": "ENABLED"}],
                "nextPageToken": "page2",
            },
            {
                "services": [{"config": {"name": "b.googleapis.com"}, "state": "ENABLED"}],
            },
        ]
    )

    result = services.enabled_services("my-proj", session=session)

    assert result == {"a.googleapis.com", "b.googleapis.com"}
    assert session.calls[1]["pageToken"] == "page2"


def test_enabled_services_raises_on_non_200():
    session = _FakeSession([{}], status_code=403)

    with pytest.raises(services.ServiceUsageCheckFailed):
        services.enabled_services("my-proj", session=session)


def test_enabled_services_wraps_unexpected_errors():
    class _BoomSession:
        def get(self, url, params=None):
            raise RuntimeError("network is down")

    with pytest.raises(services.ServiceUsageCheckFailed):
        services.enabled_services("my-proj", session=_BoomSession())
