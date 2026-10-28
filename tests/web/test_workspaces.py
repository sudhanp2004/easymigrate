"""WorkspaceManager: anonymous workspaces whose project lives in an encrypted archive."""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet, InvalidToken

from easymigrate.core.errors import EasyMigrateError
from easymigrate.web import workspaces as ws_module
from easymigrate.web.store import MemoryStore
from easymigrate.web.workspaces import WorkspaceManager

T0 = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


@pytest.fixture
def manager(settings):
    return WorkspaceManager(MemoryStore(), settings)


def test_create_returns_secret_never_stored_plain(manager):
    rec, secret = manager.create(now=T0)
    assert len(rec.id) == 32 and len(rec.external_id) == 32 and rec.ws8 == rec.id[:8]
    stored = manager.store.by_secret_hash(hashlib.sha256(secret.encode()).hexdigest())
    assert stored is not None and stored.id == rec.id
    assert secret not in json.dumps(stored.__dict__, default=str)


def test_authenticate(manager):
    rec, secret = manager.create(now=T0)
    assert manager.authenticate(secret, now=T0).id == rec.id
    assert manager.authenticate("wrong", now=T0) is None
    assert manager.authenticate(None, now=T0) is None


def test_authenticate_touches_after_five_minutes(manager):
    rec, secret = manager.create(now=T0)
    manager.authenticate(secret, now=T0 + timedelta(minutes=2))
    assert manager.store.by_secret_hash(rec.secret_hash).last_active_at == T0
    manager.authenticate(secret, now=T0 + timedelta(minutes=6))
    assert manager.store.by_secret_hash(rec.secret_hash).last_active_at == T0 + timedelta(minutes=6)


def test_commit_restore_round_trip(manager):
    rec, _ = manager.create(now=T0)
    project = manager.project_for(rec)
    project.write_text(project.dir / "note.txt", "hello")
    manager.commit(rec)

    shutil.rmtree(project.root)
    restored = manager.project_for(rec)
    assert (restored.dir / "note.txt").read_text() == "hello"


def test_archive_is_encrypted(manager, settings):
    rec, _ = manager.create(now=T0)
    project = manager.project_for(rec)
    project.write_text(project.dir / "secret-name.txt", "x")
    manager.commit(rec)

    data = manager.store.load_archive(rec.id)
    assert b"secret-name" not in data
    Fernet(settings.data_key.encode()).decrypt(data)  # decrypts with our key
    with pytest.raises(InvalidToken):
        Fernet(Fernet.generate_key()).decrypt(data)


def test_restore_marks_running_jobs_failed(manager):
    rec, _ = manager.create(now=T0)
    project = manager.project_for(rec)
    project.jobs_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "id": "j1",
        "stage": "scan",
        "status": "running",
        "started_at": T0.isoformat(),
        "finished_at": None,
        "error": None,
        "pid": 1,
    }
    (project.jobs_dir / "j1.json").write_text(json.dumps(record))
    manager.commit(rec)
    shutil.rmtree(project.root)

    restored = manager.project_for(rec)
    data = json.loads((restored.jobs_dir / "j1.json").read_text())
    assert data["status"] == "failed"
    assert data["error"] == "the server restarted while this scan was running; run it again"
    assert data["finished_at"] is not None


def test_commit_refuses_oversized_archive(manager, monkeypatch):
    monkeypatch.setattr(ws_module, "MAX_ARCHIVE_BYTES", 10)
    rec, _ = manager.create(now=T0)
    project = manager.project_for(rec)
    project.write_text(project.dir / "big.txt", "x" * 1000)
    with pytest.raises(EasyMigrateError, match="too large"):
        manager.commit(rec)


def test_cleanup_deletes_after_seven_days_only(manager):
    old, _ = manager.create(now=T0)
    fresh, _ = manager.create(now=T0 + timedelta(days=6))
    manager.project_for(old)
    manager.commit(old)

    assert manager.cleanup(now=T0 + timedelta(days=7, minutes=1)) == 1
    assert manager.store.by_secret_hash(old.secret_hash) is None
    assert manager.store.by_secret_hash(fresh.secret_hash) is not None
    assert not (manager.settings.data_dir / old.id).exists()


def test_delete_removes_record_archive_and_files(manager):
    rec, _ = manager.create(now=T0)
    manager.project_for(rec)
    manager.commit(rec)
    manager.delete(rec)
    assert manager.store.by_secret_hash(rec.secret_hash) is None
    assert manager.store.load_archive(rec.id) is None
    assert not (manager.settings.data_dir / rec.id).exists()


def test_failed_restore_never_wipes_the_archive(settings, tmp_path):
    from easymigrate.web.workspaces import WorkspaceUnreadable

    store = MemoryStore()
    manager = WorkspaceManager(store, settings)
    rec, _ = manager.create(now=T0)
    project = manager.project_for(rec)
    project.write_text(project.dir / "note.txt", "keep me")
    manager.commit(rec)
    good = store.load_archive(rec.id)
    shutil.rmtree(project.root)

    other_key = type(settings)(**{**settings.__dict__, "data_key": Fernet.generate_key().decode()})
    wrong = WorkspaceManager(store, other_key)
    with pytest.raises(WorkspaceUnreadable):
        wrong.project_for(rec)
    assert not (settings.data_dir / rec.id).exists()
    wrong.commit(rec)  # nothing local: must not store an empty archive
    assert store.load_archive(rec.id) == good
