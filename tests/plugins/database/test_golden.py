"""Golden regression tests: hand-built snapshots → expected IR (spec 2 §4, plan 1 §Tasks).

Regenerate snapshots with `uv run python scripts/make_database_fixtures.py`, then expected files
with `uv run pytest --update-golden tests/plugins/database/test_golden.py`, and review the diff.

Each fixture runs the whole scan→IR path: the network plugin normalizes first (as the registry
order requires, spec 2 §3), and the database normalizer resolves VPC/subnet/security-group native
ids through the resulting `ctx.objects`, exactly as `pipeline.analyze` would."""

from collections.abc import Callable
from pathlib import Path

import pytest

from easymigrate.core.plugin import NormalizeContext, Snapshot
from easymigrate.differences.engine import detect
from easymigrate.ir import IRDocument
from easymigrate.plugins.database.normalize import normalize as normalize_database
from easymigrate.plugins.network.normalize import normalize as normalize_network

FIXTURES = Path(__file__).parent / "fixtures"
CASES = sorted(p.name for p in FIXTURES.glob("*") if (p / "snapshot").is_dir())

# The exact `DB-*` difference keys detected for each fixture (plan 2 Part C golden
# assertion): a rule added, removed or mis-scoped for a case changes this list, so
# drift here cannot pass silently.
DETECTED_DB_KEYS: dict[str, list[str]] = {
    "custom_port": [
        "DB-BACKUP-WINDOW@database_instance:custom-port-db",
        "DB-ENGINE-VERSION@database_instance:custom-port-db",
        "DB-NAME-REUSE@database_instance:custom-port-db",
        "DB-PORT@database_instance:custom-port-db",
        "DB-PRIVATE-IP@database_instance:custom-port-db",
        "DB-SG-FIREWALL@firewall_policy:custom-port-sg",
        "DB-STORAGE-AUTORESIZE@database_instance:custom-port-db",
        "DB-TIER@database_instance:custom-port-db",
        "DB-USERS@database_instance:custom-port-db",
    ],
    "no_backups": [
        "DB-BACKUP-WINDOW@database_instance:no-backup-db",
        "DB-ENGINE-VERSION@database_instance:no-backup-db",
        "DB-NAME-REUSE@database_instance:no-backup-db",
        "DB-PRIVATE-IP@database_instance:no-backup-db",
        "DB-SG-FIREWALL@firewall_policy:no-backups-sg",
        "DB-STORAGE-AUTORESIZE@database_instance:no-backup-db",
        "DB-TIER@database_instance:no-backup-db",
        "DB-USERS@database_instance:no-backup-db",
    ],
    "old_engine_version": [
        "DB-BACKUP-WINDOW@database_instance:legacy-db",
        "DB-ENGINE-VERSION@database_instance:legacy-db",
        "DB-NAME-REUSE@database_instance:legacy-db",
        "DB-PRIVATE-IP@database_instance:legacy-db",
        "DB-SG-FIREWALL@firewall_policy:legacy-sg",
        "DB-STORAGE-AUTORESIZE@database_instance:legacy-db",
        "DB-TIER@database_instance:legacy-db",
        "DB-USERS@database_instance:legacy-db",
    ],
    "param_flags": [
        "DB-BACKUP-WINDOW@database_instance:flagged-db",
        "DB-ENGINE-VERSION@database_instance:flagged-db",
        "DB-NAME-REUSE@database_instance:flagged-db",
        "DB-PARAM-FLAGS@database_instance:flagged-db",
        "DB-PRIVATE-IP@database_instance:flagged-db",
        "DB-SG-FIREWALL@firewall_policy:flags-sg",
        "DB-STORAGE-AUTORESIZE@database_instance:flagged-db",
        "DB-TIER@database_instance:flagged-db",
        "DB-USERS@database_instance:flagged-db",
    ],
    "private_mysql_multiaz": [
        "DB-BACKUP-WINDOW@database_instance:private-mysql-db",
        "DB-ENGINE-VERSION@database_instance:private-mysql-db",
        "DB-MULTI-AZ@database_instance:private-mysql-db",
        "DB-NAME-REUSE@database_instance:private-mysql-db",
        "DB-PRIVATE-IP@database_instance:private-mysql-db",
        "DB-SG-FIREWALL@firewall_policy:mysql-sg",
        "DB-STORAGE-AUTORESIZE@database_instance:private-mysql-db",
        "DB-STORAGE-TYPE@database_instance:private-mysql-db",
        "DB-TIER@database_instance:private-mysql-db",
        "DB-USERS@database_instance:private-mysql-db",
    ],
    "psa_range_conflict": [
        "DB-BACKUP-WINDOW@database_instance:conflict-db",
        "DB-ENGINE-VERSION@database_instance:conflict-db",
        "DB-NAME-REUSE@database_instance:conflict-db",
        "DB-PRIVATE-IP@database_instance:conflict-db",
        "DB-SG-FIREWALL@firewall_policy:psa-conflict-sg",
        "DB-STORAGE-AUTORESIZE@database_instance:conflict-db",
        "DB-TIER@database_instance:conflict-db",
        "DB-USERS@database_instance:conflict-db",
    ],
    "public_postgres": [
        "DB-BACKUP-WINDOW@database_instance:public-postgres-db",
        "DB-ENGINE-VERSION@database_instance:public-postgres-db",
        "DB-LOG-EXPORTS@database_instance:public-postgres-db",
        "DB-MAINTENANCE-WINDOW@database_instance:public-postgres-db",
        "DB-NAME-REUSE@database_instance:public-postgres-db",
        "DB-PITR@database_instance:public-postgres-db",
        "DB-PUBLIC-ACCESS@database_instance:public-postgres-db",
        "DB-SG-FIREWALL@firewall_policy:public-db-sg",
        "DB-STORAGE-AUTORESIZE@database_instance:public-postgres-db",
        "DB-TIER@database_instance:public-postgres-db",
        "DB-USERS@database_instance:public-postgres-db",
    ],
    "read_replica": [
        "DB-BACKUP-WINDOW@database_instance:orders-db",
        "DB-BACKUP-WINDOW@database_instance:orders-db-replica",
        "DB-ENGINE-VERSION@database_instance:orders-db",
        "DB-ENGINE-VERSION@database_instance:orders-db-replica",
        "DB-NAME-REUSE@database_instance:orders-db",
        "DB-NAME-REUSE@database_instance:orders-db-replica",
        "DB-PRIVATE-IP@database_instance:orders-db",
        "DB-PRIVATE-IP@database_instance:orders-db-replica",
        "DB-REPLICA@database_instance:orders-db-replica",
        "DB-SG-FIREWALL@firewall_policy:orders-sg",
        "DB-STORAGE-AUTORESIZE@database_instance:orders-db",
        "DB-STORAGE-AUTORESIZE@database_instance:orders-db-replica",
        "DB-TIER@database_instance:orders-db",
        "DB-TIER@database_instance:orders-db-replica",
        "DB-USERS@database_instance:orders-db",
        "DB-USERS@database_instance:orders-db-replica",
    ],
    "unsupported_engines": [],
}


def _kinds(doc: IRDocument, kind: str) -> list:
    return [o for o in doc.objects if o.kind == kind]


# One key fact per case, so a wrong golden file cannot silently pass review.
KEY_FACTS: dict[str, Callable[[IRDocument], bool]] = {
    "public_postgres": lambda doc: _kinds(doc, "database_instance")[0].network.publicly_accessible,
    "private_mysql_multiaz": lambda doc: _kinds(doc, "database_instance")[0].high_availability,
    "read_replica": lambda doc: any(
        i.role == "read_replica" and i.replica_of == "database_instance:orders-db"
        for i in _kinds(doc, "database_instance")
    ),
    "param_flags": lambda doc: (
        _kinds(doc, "database_instance")[0].parameters
        == {
            "log_min_duration_statement": "1000",
            "rds.force_ssl": "1",
            "shared_buffers": "{DBInstanceClassMemory/32768}",
        }
    ),
    "unsupported_engines": lambda doc: (
        _kinds(doc, "database_instance") == []
        and len([f for f in doc.findings if f.level == "unsupported"]) == 3
    ),
    "custom_port": lambda doc: _kinds(doc, "database_instance")[0].network.port == 5433,
    "old_engine_version": lambda doc: (
        _kinds(doc, "database_instance")[0].engine_version == "9.6.24"
    ),
    "psa_range_conflict": lambda doc: _kinds(doc, "network")[0].cidr_blocks == ["10.64.0.0/16"],
    "no_backups": lambda doc: _kinds(doc, "database_instance")[0].backup.retention_days == 0,
}


def _normalize(case: str) -> IRDocument:
    ctx = NormalizeContext(account_id="123456789012", region="ap-south-1", strict=True)
    snapshot = Snapshot.from_dir(FIXTURES / case / "snapshot")
    network_objects = normalize_network(snapshot, ctx)
    ctx.objects.update({obj.id: obj for obj in network_objects})
    database_objects = normalize_database(snapshot, ctx)
    return IRDocument(
        source={"cloud": "aws", "account_id": "123456789012", "region": "ap-south-1"},
        objects=[*network_objects, *database_objects],
        findings=ctx.findings,
    )


def test_every_spec_case_has_a_fixture():
    assert set(CASES) == set(KEY_FACTS)
    assert set(CASES) == set(DETECTED_DB_KEYS)
    assert CASES == [
        "custom_port",
        "no_backups",
        "old_engine_version",
        "param_flags",
        "private_mysql_multiaz",
        "psa_range_conflict",
        "public_postgres",
        "read_replica",
        "unsupported_engines",
    ]


@pytest.mark.parametrize("case", CASES)
def test_golden(case, update_golden):
    doc = _normalize(case)
    actual = doc.model_dump_json(indent=2) + "\n"
    expected_path = FIXTURES / case / "expected_ir.json"

    if update_golden:
        expected_path.write_text(actual)

    assert expected_path.exists(), "missing golden file; run pytest --update-golden"
    assert actual == expected_path.read_text()
    assert KEY_FACTS[case](doc), f"key fact for {case} does not hold"

    detected_db_keys = sorted(d.key for d in detect(doc) if d.rule_id.startswith("DB-"))
    assert detected_db_keys == DETECTED_DB_KEYS[case]
