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
from easymigrate.ir import IRDocument
from easymigrate.plugins.database.normalize import normalize as normalize_database
from easymigrate.plugins.network.normalize import normalize as normalize_network

FIXTURES = Path(__file__).parent / "fixtures"
CASES = sorted(p.name for p in FIXTURES.glob("*") if (p / "snapshot").is_dir())


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
