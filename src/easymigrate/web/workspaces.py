"""Anonymous visitor workspaces.

A workspace's `.easymigrate/` project lives in a local directory that is only a cache: after
every change it is archived (tar.gz), encrypted with Fernet and stored, and a fresh instance
(Render restarts and sleeps) restores it on first use. No AWS credential is ever stored, only
the role ARN the visitor created and the external ID we generated for it.
"""

from __future__ import annotations

import hashlib
import io
import secrets
import shutil
import tarfile
import threading
from datetime import UTC, datetime, timedelta

from cryptography.fernet import Fernet, InvalidToken

from easymigrate.core.errors import EasyMigrateError
from easymigrate.core.project import Project
from easymigrate.ui.jobs import JobRecord
from easymigrate.web.settings import WebSettings
from easymigrate.web.store import Store, WorkspaceRecord

RETENTION = timedelta(days=7)
TOUCH_EVERY = timedelta(minutes=5)
MAX_ARCHIVE_BYTES = 50 * 1024 * 1024
_RESTARTED = "the server restarted while this {stage} was running; run it again"


class WorkspaceUnreadable(Exception):
    """The stored archive cannot be decrypted or unpacked (data key changed, corrupt blob).
    The archive is left untouched; the visitor starts a new workspace."""


def secret_hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


class WorkspaceManager:
    def __init__(self, store: Store, settings: WebSettings) -> None:
        self.store = store
        self.settings = settings
        self._fernet = Fernet(settings.data_key.encode())
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock(self, workspace_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(workspace_id, threading.Lock())

    def _root(self, rec: WorkspaceRecord):
        return self.settings.data_dir / rec.id

    def create(self, now: datetime | None = None) -> tuple[WorkspaceRecord, str]:
        now = now or datetime.now(UTC)
        secret = secrets.token_urlsafe(32)
        rec = WorkspaceRecord(
            id=secrets.token_hex(16),
            secret_hash=secret_hash(secret),
            external_id=secrets.token_hex(16),
            region=None,
            role_arn=None,
            account_id=None,
            created_at=now,
            last_active_at=now,
        )
        self.store.create(rec)
        return rec, secret

    def authenticate(
        self, secret: str | None, now: datetime | None = None
    ) -> WorkspaceRecord | None:
        if not secret:
            return None
        rec = self.store.by_secret_hash(secret_hash(secret))
        if rec is None:
            return None
        now = now or datetime.now(UTC)
        if now - rec.last_active_at >= TOUCH_EVERY:
            rec.last_active_at = now
            self.store.update(rec)
        return rec

    def project_for(self, rec: WorkspaceRecord) -> Project:
        root = self._root(rec)
        with self._lock(rec.id):
            if not root.is_dir():
                data = self.store.load_archive(rec.id)
                if data is None:
                    root.mkdir(parents=True, exist_ok=True)
                else:
                    self._restore(root, data)
        project = Project(root)
        project.init()
        return project

    def _restore(self, root, data: bytes) -> None:
        """Unpack into a scratch directory and move it into place only once complete: a
        half-restored directory would look like an empty workspace, and the next commit
        would overwrite the good archive with it."""
        scratch = root.with_name(f"{root.name}.restore-{secrets.token_hex(4)}")
        try:
            raw = self._fernet.decrypt(data)
            scratch.mkdir(parents=True)
            with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
                tar.extractall(scratch, filter="data")
            _fail_running_jobs(Project(scratch))
            scratch.rename(root)
        except (InvalidToken, tarfile.TarError, OSError, EOFError) as exc:
            shutil.rmtree(scratch, ignore_errors=True)
            raise WorkspaceUnreadable(str(exc) or type(exc).__name__) from None

    def commit(self, rec: WorkspaceRecord) -> None:
        root = self._root(rec)
        with self._lock(rec.id):
            if not root.is_dir():
                return
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
                for path in sorted(root.iterdir()):
                    tar.add(path, arcname=path.name)
            raw = buffer.getvalue()
            if len(raw) > MAX_ARCHIVE_BYTES:
                raise EasyMigrateError(
                    "this workspace is too large to keep (over 50 MB compressed); the scan "
                    "covers more than the hosted version supports, use the CLI instead"
                )
            self.store.save_archive(rec.id, self._fernet.encrypt(raw))

    def delete(self, rec: WorkspaceRecord) -> None:
        self.store.delete(rec.id)
        with self._lock(rec.id):
            shutil.rmtree(self._root(rec), ignore_errors=True)

    def cleanup(self, now: datetime | None = None) -> int:
        now = now or datetime.now(UTC)
        stale = self.store.inactive_since(now - RETENTION)
        for workspace_id in stale:
            self.store.delete(workspace_id)
            shutil.rmtree(self.settings.data_dir / workspace_id, ignore_errors=True)
        self.store.prune_rate_hits(now - timedelta(days=1))
        return len(stale)


def _fail_running_jobs(project: Project) -> None:
    """A restored archive came from a process that is gone, so any job it left `running`
    is dead. `JobStore.reap_stale` cannot tell: on a container the new server often gets
    the very same pid."""
    jobs_dir = project.jobs_dir
    if not jobs_dir.is_dir():
        return
    for path in jobs_dir.glob("*.json"):
        try:
            record = JobRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if record.status != "running":
            continue
        failed = record.model_copy(
            update={
                "status": "failed",
                "finished_at": datetime.now(UTC),
                "error": _RESTARTED.format(stage=record.stage),
            }
        )
        path.write_text(failed.model_dump_json(), encoding="utf-8")
