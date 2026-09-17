"""Unit tests for the password-resolution and GCP-services-preflight helpers (plan 4,
spec §2.2 / §6.4). These are pure functions of an `IRDocument`, tested here directly
rather than through a full scan->plan(), which is the layer I2 found untested:
`_resolve_db_passwords`'s resolution order, its slug derivation, its validation rules,
and `_required_gcp_services`'s two conditions."""

from __future__ import annotations

import pytest

from easymigrate.core.errors import EasyMigrateError
from easymigrate.core.pipeline import (
    _db_password_env_var,
    _required_gcp_services,
    _resolve_db_passwords,
    _validate_db_password,
)
from easymigrate.ir.base import IRDocument, SourceRef
from easymigrate.ir.database import DatabaseInstance, DbCompute, DbNetwork, DbStorage


def _src(id_: str) -> SourceRef:
    return SourceRef(cloud="aws", type="db", id=id_, region="ap-south-1")


def _instance(
    id_: str,
    *,
    role: str = "primary",
    replica_of: str | None = None,
    excluded: str | None = None,
    hints: dict | None = None,
) -> DatabaseInstance:
    instance = DatabaseInstance(
        id=f"database_instance:{id_}",
        name=id_,
        source=_src(f"db-{id_}"),
        engine="postgres",
        engine_version="15.4",
        role=role,
        replica_of=replica_of,
        compute=DbCompute(class_name="db.t3.micro", vcpus=1, memory_mib=1024),
        storage=DbStorage(size_gib=20, type="gp2"),
        network=DbNetwork(publicly_accessible=True, port=5432),
    )
    if excluded is not None:
        instance = instance.model_copy(update={"excluded": excluded})
    if hints:
        instance = instance.model_copy(update={"target_hints": hints})
    return instance


def _doc(*instances: DatabaseInstance) -> IRDocument:
    return IRDocument(source={}, objects=list(instances))


# ---- _db_password_env_var -----------------------------------------------------------


def test_db_password_env_var_uppercases_and_replaces_non_alnum():
    assert (
        _db_password_env_var("database_instance:app-db.prod")
        == "EASYMIGRATE_DB_PASSWORD_APP_DB_PROD"
    )


# ---- _validate_db_password -----------------------------------------------------------


def test_validate_db_password_accepts_ordinary_password():
    _validate_db_password("database_instance:x", "s3cret!")  # should not raise


def test_validate_db_password_rejects_empty():
    with pytest.raises(EasyMigrateError, match="between 1 and"):
        _validate_db_password("database_instance:x", "")


def test_validate_db_password_rejects_too_long():
    with pytest.raises(EasyMigrateError, match="between 1 and"):
        _validate_db_password("database_instance:x", "a" * 1025)


def test_validate_db_password_rejects_leading_whitespace():
    with pytest.raises(EasyMigrateError, match="whitespace"):
        _validate_db_password("database_instance:x", " secret")


def test_validate_db_password_rejects_trailing_whitespace():
    with pytest.raises(EasyMigrateError, match="whitespace"):
        _validate_db_password("database_instance:x", "secret ")


def test_validate_db_password_rejects_nul():
    with pytest.raises(EasyMigrateError, match="NUL"):
        _validate_db_password("database_instance:x", "sec\x00ret")


def test_validate_db_password_rejects_newline():
    with pytest.raises(EasyMigrateError, match="newline"):
        _validate_db_password("database_instance:x", "sec\nret")


def test_validate_db_password_rejects_carriage_return():
    with pytest.raises(EasyMigrateError, match="newline"):
        _validate_db_password("database_instance:x", "sec\rret")


# ---- _resolve_db_passwords: resolution order -----------------------------------------


def test_resolve_db_passwords_prefers_explicit_flag_over_env_and_prompt(monkeypatch):
    monkeypatch.setenv(_db_password_env_var("database_instance:app"), "from-env")
    doc = _doc(_instance("app"))

    resolved = _resolve_db_passwords(
        doc,
        {"database_instance:app": "from-flag"},
        ask_password=lambda ir_id: "from-prompt",
    )

    assert resolved == {"database_instance:app": "from-flag"}


def test_resolve_db_passwords_falls_back_to_env_var(monkeypatch):
    monkeypatch.setenv(_db_password_env_var("database_instance:app"), "from-env")
    doc = _doc(_instance("app"))

    resolved = _resolve_db_passwords(doc, None, ask_password=None)

    assert resolved == {"database_instance:app": "from-env"}


def test_resolve_db_passwords_falls_back_to_prompt_when_no_flag_or_env(monkeypatch):
    monkeypatch.delenv(_db_password_env_var("database_instance:app"), raising=False)
    doc = _doc(_instance("app"))

    resolved = _resolve_db_passwords(doc, None, ask_password=lambda ir_id: "from-prompt")

    assert resolved == {"database_instance:app": "from-prompt"}


def test_resolve_db_passwords_raises_naming_every_missing_instance_and_both_routes(
    monkeypatch,
):
    monkeypatch.delenv(_db_password_env_var("database_instance:one"), raising=False)
    monkeypatch.delenv(_db_password_env_var("database_instance:two"), raising=False)
    doc = _doc(_instance("one"), _instance("two"))

    with pytest.raises(EasyMigrateError) as exc_info:
        _resolve_db_passwords(doc, None, ask_password=None)

    message = str(exc_info.value)
    assert "database_instance:one" in message
    assert "database_instance:two" in message
    assert "--db-password database_instance:one=<value>" in message
    assert _db_password_env_var("database_instance:one") in message
    assert "--db-password database_instance:two=<value>" in message
    assert _db_password_env_var("database_instance:two") in message


def test_resolve_db_passwords_skips_replicas_and_excluded_instances(monkeypatch):
    monkeypatch.delenv(_db_password_env_var("database_instance:primary"), raising=False)
    primary = _instance("primary")
    replica = _instance("replica", role="read_replica", replica_of="database_instance:primary")
    excluded = _instance("excluded", excluded="manual review")

    resolved = _resolve_db_passwords(
        _doc(primary, replica, excluded),
        {"database_instance:primary": "pw"},
        ask_password=None,
    )

    assert resolved == {"database_instance:primary": "pw"}


# ---- _required_gcp_services -----------------------------------------------------------


def test_required_gcp_services_empty_with_no_database_instances():
    assert _required_gcp_services(_doc()) == set()


def test_required_gcp_services_sqladmin_only_without_psa():
    doc = _doc(_instance("app"))

    assert _required_gcp_services(doc) == {"sqladmin.googleapis.com"}


def test_required_gcp_services_adds_servicenetworking_when_psa_used():
    doc = _doc(_instance("app", hints={"psa": {"range": "10.64.0.0", "prefix_length": 24}}))

    assert _required_gcp_services(doc) == {
        "sqladmin.googleapis.com",
        "servicenetworking.googleapis.com",
    }


def test_required_gcp_services_ignores_excluded_instances():
    doc = _doc(_instance("app", excluded="manual review"))

    assert _required_gcp_services(doc) == set()
