import pytest
from pydantic import ValidationError

from easymigrate.ir import IRDocument, SourceRef, intent_class
from easymigrate.ir.database import (
    DatabaseInstance,
    DbBackup,
    DbCompute,
    DbMaintenance,
    DbNetwork,
    DbStorage,
)


def src(type_: str, id_: str) -> SourceRef:
    return SourceRef(cloud="aws", type=type_, id=id_, region="ap-south-1")


def instance(name: str = "db1", **overrides) -> DatabaseInstance:
    fields = dict(
        engine="postgres",
        engine_version="15.5",
        compute=DbCompute(class_name="db.t3.micro"),
        storage=DbStorage(size_gib=20, type="gp2"),
        network=DbNetwork(port=5432),
    )
    fields.update(overrides)
    return DatabaseInstance(
        id=f"database_instance:{name}", name=name, source=src("db", f"db-{name}"), **fields
    )


def document(*objects) -> IRDocument:
    return IRDocument(
        source={"cloud": "aws", "account_id": "123456789012", "region": "ap-south-1"},
        objects=list(objects),
    )


def test_database_kind_is_registered():
    assert intent_class("database_instance") is DatabaseInstance


def test_minimal_instance_round_trips_through_document():
    doc = document(instance())
    raw = doc.model_dump_json()
    parsed = IRDocument.model_validate_json(raw)
    assert parsed.get("database_instance:db1").engine == "postgres"


def test_references_include_network_subnets_policies_and_replica():
    db = instance(
        network=DbNetwork(
            network="network:main",
            subnets=["subnet:a", "subnet:b"],
            firewall_policies=["firewall_policy:web"],
            port=5432,
        ),
        role="read_replica",
        replica_of="database_instance:primary",
    )
    refs = db.references()
    assert set(refs) == {
        "network:main",
        "subnet:a",
        "subnet:b",
        "firewall_policy:web",
        "database_instance:primary",
    }


def test_references_omit_external_replica_of():
    db = instance(role="read_replica", replica_of="external:some-primary")
    assert "external:some-primary" not in db.references()


def test_read_replica_requires_replica_of():
    with pytest.raises(ValidationError):
        instance(role="read_replica")


def test_primary_forbids_replica_of():
    with pytest.raises(ValidationError):
        instance(role="primary", replica_of="database_instance:other")


def test_iops_forbidden_on_gp2():
    with pytest.raises(ValidationError):
        instance(storage=DbStorage(size_gib=20, type="gp2", iops=3000))


def test_iops_allowed_on_gp3():
    db = instance(storage=DbStorage(size_gib=20, type="gp3", iops=3000))
    assert db.storage.iops == 3000


def test_throughput_forbidden_on_standard():
    with pytest.raises(ValidationError):
        instance(storage=DbStorage(size_gib=20, type="standard", throughput_mibps=125))


@pytest.mark.parametrize("port", [0, 70000])
def test_invalid_port_raises(port):
    with pytest.raises(ValidationError):
        instance(network=DbNetwork(port=port))


def test_unknown_extra_field_raises():
    with pytest.raises(ValidationError):
        DatabaseInstance(
            id="database_instance:db1",
            name="db1",
            source=src("db", "db-db1"),
            engine="postgres",
            engine_version="15.5",
            compute=DbCompute(class_name="db.t3.micro"),
            storage=DbStorage(size_gib=20, type="gp2"),
            network=DbNetwork(port=5432),
            bogus="field",
        )


def test_invalid_replica_of_format_raises():
    with pytest.raises(ValidationError):
        instance(role="read_replica", replica_of="not-a-valid-ref")


def test_backup_and_maintenance_defaults():
    db = instance()
    assert db.backup == DbBackup()
    assert db.maintenance == DbMaintenance()
    assert db.parameters == {}
    assert db.log_exports == []
