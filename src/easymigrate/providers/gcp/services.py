"""Service Usage API check for `plan`'s preflight (spec §6.4, plan 4). The only module
allowed to import `google.auth` for this purpose besides `easymigrate.core.registry`,
which wires it in."""

from __future__ import annotations

import time
from typing import Any

import google.auth
from google.auth.transport.requests import AuthorizedSession

_LIST_URL = "https://serviceusage.googleapis.com/v1/projects/{project}/services"
_PAGE_SIZE = 200


class ServiceUsageCheckFailed(Exception):
    """The Service Usage API could not be queried (e.g. no permission to read it, or a
    network error). Callers treat this as "the check could not run", not "the API is
    missing": a user whose APIs are already enabled must not be blocked by a missing
    read permission."""


def enabled_services(gcp_project: str, *, session: Any = None) -> set[str]:
    """Every API enabled on `gcp_project`, via serviceusage.services.list filtered to
    state ENABLED, following pagination to the end."""
    if session is None:
        credentials, _ = google.auth.default()
        session = AuthorizedSession(credentials)

    enabled: set[str] = set()
    params: dict[str, Any] = {"filter": "state:ENABLED", "pageSize": _PAGE_SIZE}
    url = _LIST_URL.format(project=gcp_project)
    try:
        while True:
            response = session.get(url, params=params)
            if response.status_code != 200:
                raise ServiceUsageCheckFailed(
                    f"Service Usage API returned {response.status_code}: {response.text}"
                )
            data = response.json()
            for service in data.get("services", []):
                name = service.get("config", {}).get("name") or service.get("name", "")
                enabled.add(name.rsplit("/", 1)[-1])
            token = data.get("nextPageToken")
            if not token:
                return enabled
            params["pageToken"] = token
    except ServiceUsageCheckFailed:
        raise
    except Exception as exc:
        raise ServiceUsageCheckFailed(str(exc)) from exc


_PROJECT_URL = "https://compute.googleapis.com/compute/v1/projects/{project}"


def quota_headroom(gcp_project: str, metric: str, *, session: Any = None) -> int | None:
    """How many more of `metric` (a Compute Engine project quota, e.g. SECURITY_POLICIES) the
    project can create, or None when the quota could not be read. New projects often have a
    Cloud Armor limit of zero (live run, 2026-09-22)."""
    try:
        if session is None:
            credentials, _ = google.auth.default()
            session = AuthorizedSession(credentials)
        response = session.get(_PROJECT_URL.format(project=gcp_project))
        if response.status_code != 200:
            return None
        for quota in response.json().get("quotas", []):
            if quota.get("metric") == metric:
                return int(quota.get("limit", 0) - quota.get("usage", 0))
    except Exception:
        return None
    return None


_NETWORK_URL = _PROJECT_URL + "/global/networks/{network}"
_OPERATION_URL = _PROJECT_URL + "/global/operations/{operation}"


def remove_peering(
    gcp_project: str,
    network: str,
    peering: str,
    *,
    session: Any = None,
    sleep: Any = time.sleep,
    timeout: float = 300,
) -> None:
    """Delete VPC peering `peering` from `network` and wait for it to finish.

    `tofu destroy` cannot delete a private-services connection while Cloud SQL's producer
    network still holds it, and `gcloud services vpc-peerings delete` fails the same way;
    removing the peering from our side works at once (live teardowns, 2026-09-16 onwards).
    A peering that is already gone counts as removed."""
    if session is None:
        credentials, _ = google.auth.default()
        session = AuthorizedSession(credentials)
    url = _NETWORK_URL.format(project=gcp_project, network=network) + "/removePeering"
    response = session.post(url, json={"name": peering})
    if response.status_code == 404 or (
        response.status_code == 400 and "not found" in response.text.lower()
    ):
        return
    if response.status_code != 200:
        raise RuntimeError(f"removePeering returned {response.status_code}: {response.text}")
    operation = response.json()
    waited = 0.0
    while operation.get("status") != "DONE":
        if waited >= timeout:
            raise RuntimeError(f"removing peering {peering} from {network} did not finish")
        sleep(2)
        waited += 2
        polled = session.get(
            _OPERATION_URL.format(project=gcp_project, operation=operation["name"])
        )
        if polled.status_code != 200:
            raise RuntimeError(f"operation poll returned {polled.status_code}: {polled.text}")
        operation = polled.json()
    if operation.get("error"):
        raise RuntimeError(f"removing peering {peering} failed: {operation['error']}")
