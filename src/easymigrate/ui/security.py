"""Loopback-only origin/host checks and the launch token (spec §6).

Any website open in the user's browser can send requests to `127.0.0.1`, and DNS
rebinding defeats a naive `Origin` check by making the browser send an attacker's
hostname while really talking to the loopback address. `Security` closes both: every
route checks `Origin`/`Host` against the expected loopback authority, and every
mutating route additionally requires a per-run token that a cross-site request can
never read.
"""

from __future__ import annotations

import secrets

from fastapi import HTTPException, Request

TOKEN_HEADER = "X-EasyMigrate-Token"

_FORBIDDEN = "forbidden"


def new_token() -> str:
    return secrets.token_urlsafe(32)


class Security:
    def __init__(self, token: str, host: str, port: int) -> None:
        self._token = token
        self._allowed_hosts = {f"{host}:{port}", f"localhost:{port}"}
        self._allowed_origins = {f"http://{authority}" for authority in self._allowed_hosts}

    def check_origin(self, origin: str | None, host_header: str | None) -> None:
        if host_header not in self._allowed_hosts:
            raise HTTPException(status_code=403, detail=_FORBIDDEN)
        if origin is not None and origin not in self._allowed_origins:
            raise HTTPException(status_code=403, detail=_FORBIDDEN)

    def check_token(self, supplied: str | None) -> None:
        if not supplied or not secrets.compare_digest(supplied, self._token):
            raise HTTPException(status_code=403, detail=_FORBIDDEN)

    def require_origin(self, request: Request) -> None:
        self.check_origin(request.headers.get("origin"), request.headers.get("host"))

    def require_token(self, request: Request) -> None:
        self.check_token(request.headers.get(TOKEN_HEADER))
