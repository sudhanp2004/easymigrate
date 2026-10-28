"""Where hosted workspaces live: their record, their encrypted project archive, and the
rate-limit counters. `PostgresStore` in production (Neon), `MemoryStore` in tests and
local development."""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol


@dataclass
class WorkspaceRecord:
    id: str
    secret_hash: str
    external_id: str
    region: str | None
    role_arn: str | None
    account_id: str | None
    created_at: datetime
    last_active_at: datetime

    @property
    def ws8(self) -> str:
        return self.id[:8]


class Store(Protocol):
    def create(self, rec: WorkspaceRecord) -> None: ...
    def by_secret_hash(self, secret_hash: str) -> WorkspaceRecord | None: ...
    def update(self, rec: WorkspaceRecord) -> None: ...
    def delete(self, workspace_id: str) -> None: ...
    def save_archive(self, workspace_id: str, data: bytes) -> None: ...
    def load_archive(self, workspace_id: str) -> bytes | None: ...
    def inactive_since(self, cutoff: datetime) -> list[str]: ...
    def hit(self, key: str, window_s: int, limit: int, now: datetime | None = None) -> bool: ...
    def prune_rate_hits(self, cutoff: datetime) -> None: ...


def _now() -> datetime:
    return datetime.now(UTC)


class MemoryStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, WorkspaceRecord] = {}
        self._archives: dict[str, bytes] = {}
        self._hits: dict[str, list[datetime]] = {}

    def create(self, rec: WorkspaceRecord) -> None:
        with self._lock:
            self._records[rec.id] = replace(rec)

    def by_secret_hash(self, secret_hash: str) -> WorkspaceRecord | None:
        with self._lock:
            for rec in self._records.values():
                if rec.secret_hash == secret_hash:
                    return replace(rec)
        return None

    def update(self, rec: WorkspaceRecord) -> None:
        with self._lock:
            if rec.id in self._records:
                self._records[rec.id] = replace(rec)

    def delete(self, workspace_id: str) -> None:
        with self._lock:
            self._records.pop(workspace_id, None)
            self._archives.pop(workspace_id, None)

    def save_archive(self, workspace_id: str, data: bytes) -> None:
        with self._lock:
            if workspace_id in self._records:
                self._archives[workspace_id] = data

    def load_archive(self, workspace_id: str) -> bytes | None:
        with self._lock:
            return self._archives.get(workspace_id)

    def inactive_since(self, cutoff: datetime) -> list[str]:
        with self._lock:
            return sorted(r.id for r in self._records.values() if r.last_active_at < cutoff)

    def hit(self, key: str, window_s: int, limit: int, now: datetime | None = None) -> bool:
        now = now or _now()
        cutoff = now - timedelta(seconds=window_s)
        with self._lock:
            hits = [at for at in self._hits.get(key, []) if at > cutoff]
            allowed = len(hits) < limit
            if allowed:
                hits.append(now)
            self._hits[key] = hits
            return allowed

    def prune_rate_hits(self, cutoff: datetime) -> None:
        with self._lock:
            self._hits = {k: [at for at in v if at > cutoff] for k, v in self._hits.items() if v}


_SCHEMA = """
CREATE TABLE IF NOT EXISTS workspaces (
    id text PRIMARY KEY,
    secret_hash text UNIQUE NOT NULL,
    external_id text NOT NULL,
    region text,
    role_arn text,
    account_id text,
    created_at timestamptz NOT NULL,
    last_active_at timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS archives (
    workspace_id text PRIMARY KEY REFERENCES workspaces(id) ON DELETE CASCADE,
    data bytea NOT NULL,
    updated_at timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS rate_hits (key text NOT NULL, at timestamptz NOT NULL);
CREATE INDEX IF NOT EXISTS rate_hits_key_at ON rate_hits (key, at);
"""

_COLUMNS = "id, secret_hash, external_id, region, role_arn, account_id, created_at, last_active_at"


class PostgresStore:
    """One short connection per call: traffic is low and Neon suspends idle computes, so a
    pool would mostly hold dead connections."""

    def __init__(self, url: str) -> None:
        import psycopg

        self._psycopg = psycopg
        self._url = url
        with self._connect() as conn:
            conn.execute(_SCHEMA)

    def _connect(self):
        return self._psycopg.connect(self._url, autocommit=True)

    def reset_for_tests(self) -> None:
        with self._connect() as conn:
            conn.execute("TRUNCATE workspaces, archives, rate_hits")

    def create(self, rec: WorkspaceRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                f"INSERT INTO workspaces ({_COLUMNS}) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    rec.id,
                    rec.secret_hash,
                    rec.external_id,
                    rec.region,
                    rec.role_arn,
                    rec.account_id,
                    rec.created_at,
                    rec.last_active_at,
                ),
            )

    def by_secret_hash(self, secret_hash: str) -> WorkspaceRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM workspaces WHERE secret_hash = %s", (secret_hash,)
            ).fetchone()
        return WorkspaceRecord(*row) if row else None

    def update(self, rec: WorkspaceRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE workspaces SET region = %s, role_arn = %s, account_id = %s, "
                "last_active_at = %s WHERE id = %s",
                (rec.region, rec.role_arn, rec.account_id, rec.last_active_at, rec.id),
            )

    def delete(self, workspace_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM workspaces WHERE id = %s", (workspace_id,))

    def save_archive(self, workspace_id: str, data: bytes) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO archives (workspace_id, data, updated_at) "
                "SELECT id, %s, now() FROM workspaces WHERE id = %s "
                "ON CONFLICT (workspace_id) DO UPDATE SET data = EXCLUDED.data, "
                "updated_at = EXCLUDED.updated_at",
                (data, workspace_id),
            )

    def load_archive(self, workspace_id: str) -> bytes | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT data FROM archives WHERE workspace_id = %s", (workspace_id,)
            ).fetchone()
        return bytes(row[0]) if row else None

    def inactive_since(self, cutoff: datetime) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id FROM workspaces WHERE last_active_at < %s ORDER BY id", (cutoff,)
            ).fetchall()
        return [row[0] for row in rows]

    def hit(self, key: str, window_s: int, limit: int, now: datetime | None = None) -> bool:
        now = now or _now()
        cutoff = now - timedelta(seconds=window_s)
        with self._connect() as conn, conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (key,))
            conn.execute("DELETE FROM rate_hits WHERE key = %s AND at <= %s", (key, cutoff))
            (count,) = conn.execute(
                "SELECT count(*) FROM rate_hits WHERE key = %s", (key,)
            ).fetchone()
            if count >= limit:
                return False
            conn.execute("INSERT INTO rate_hits (key, at) VALUES (%s, %s)", (key, now))
            return True

    def prune_rate_hits(self, cutoff: datetime) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM rate_hits WHERE at <= %s", (cutoff,))
