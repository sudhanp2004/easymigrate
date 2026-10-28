"""Store contract: MemoryStore always, PostgresStore when a test database is configured."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from easymigrate.web.store import WorkspaceRecord

T0 = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


def _rec(id_="a" * 32, secret_hash="h1") -> WorkspaceRecord:
    return WorkspaceRecord(
        id=id_,
        secret_hash=secret_hash,
        external_id="e" * 32,
        region=None,
        role_arn=None,
        account_id=None,
        created_at=T0,
        last_active_at=T0,
    )


def test_create_lookup_update(store):
    rec = _rec()
    store.create(rec)
    assert store.by_secret_hash("h1") == rec
    assert store.by_secret_hash("nope") is None

    rec.region, rec.role_arn, rec.account_id = "ap-south-1", "arn:r", "123456789012"
    store.update(rec)
    assert store.by_secret_hash("h1").account_id == "123456789012"


def test_archive_round_trip_and_delete_cascades(store):
    store.create(_rec())
    assert store.load_archive("a" * 32) is None
    store.save_archive("a" * 32, b"one")
    store.save_archive("a" * 32, b"two")
    assert store.load_archive("a" * 32) == b"two"

    store.delete("a" * 32)
    assert store.by_secret_hash("h1") is None
    assert store.load_archive("a" * 32) is None


def test_inactive_since(store):
    old = _rec("o" * 32, "ho")
    new = _rec("n" * 32, "hn")
    new.last_active_at = T0 + timedelta(days=8)
    store.create(old)
    store.create(new)
    assert store.inactive_since(T0 + timedelta(days=1)) == ["o" * 32]


def test_hit_limits_within_window(store):
    for i in range(3):
        assert store.hit("ip:1", 3600, 3, now=T0 + timedelta(minutes=i)) is True
    assert store.hit("ip:1", 3600, 3, now=T0 + timedelta(minutes=5)) is False
    assert store.hit("ip:2", 3600, 3, now=T0 + timedelta(minutes=5)) is True
    # an hour after the first hit, its slot is free again
    assert store.hit("ip:1", 3600, 3, now=T0 + timedelta(minutes=61)) is True
