"""Unit tests for the database normalizer: hand-built snapshots via aws_builders.py."""

from easymigrate.core.plugin import NormalizeContext, Snapshot, UnmodelledData
from easymigrate.ir.database import DatabaseInstance
from easymigrate.plugins.database.normalize import normalize
from easymigrate.plugins.network.normalize import normalize as network_normalize
from tests.plugins.database import aws_builders as b
from tests.plugins.network import aws_builders as nb


def _ctx(strict: bool = False) -> NormalizeContext:
    return NormalizeContext(account_id="123456789012", region="ap-south-1", strict=strict)


def _network_objects(
    ctx,
    *,
    vpc_id="vpc-1",
    cidr="10.0.0.0/16",
    subnet_ids=("subnet-1", "subnet-2"),
    vpc_name="test-vpc",
):
    """Populate ctx.objects with a VPC + subnets + a security group, as NetworkPlugin would."""
    vpcs = [nb.vpc(vpc_id, cidr, name=vpc_name)]
    subnets = [
        nb.subnet(sid, vpc_id, f"10.0.{i}.0/24", name=f"{vpc_name}-subnet-{i}")
        for i, sid in enumerate(subnet_ids)
    ]
    sg = nb.security_group("sg-1", vpc_id, "db-sg", name="db-sg")

    net_snapshot = Snapshot(
        {
            "network.vpcs": vpcs,
            "network.subnets": subnets,
            "network.vpc_attributes": {
                vpc_id: {"enableDnsHostnames": True, "enableDnsSupport": True}
            },
            "network.security_groups": [sg],
        }
    )
    objects = network_normalize(net_snapshot, ctx)
    ctx.objects.update({o.id: o for o in objects})
    return objects


def test_testbed_shape_public_postgres():
    ctx = _ctx()
    _network_objects(ctx)
    snap = b.snapshot(
        db_instances=[
            b.db_instance(
                "testbed-db",
                name="testbed-db",
                publicly_accessible=True,
                subnet_group_name="subgrp",
                allocated_storage=20,
                instance_class="db.t3.micro",
            )
        ],
        db_subnet_groups=[b.db_subnet_group("subgrp", "vpc-1", ("subnet-1", "subnet-2"))],
        instance_types=[b.instance_type("t3.micro", vcpus=2, memory_mib=1024)],
    )
    objs = normalize(snap, ctx)
    assert len(objs) == 1
    inst = objs[0]
    assert isinstance(inst, DatabaseInstance)
    assert inst.id == "database_instance:testbed-db"
    assert inst.engine == "postgres"
    assert inst.compute.class_name == "db.t3.micro"
    assert inst.compute.vcpus == 2
    assert inst.compute.memory_mib == 1024
    assert inst.storage.size_gib == 20
    assert inst.network.publicly_accessible is True
    assert inst.network.network == "network:test-vpc"
    assert inst.network.subnets == ["subnet:test-vpc-subnet-0", "subnet:test-vpc-subnet-1"]


def test_private_mysql_resolves_network_subnets_and_firewall_and_updates_applies_to():
    ctx = _ctx()
    objects = _network_objects(ctx)
    policy = next(o for o in objects if o.kind == "firewall_policy")
    assert policy.applies_to == []

    snap = b.snapshot(
        db_instances=[
            b.db_instance(
                "private-mysql",
                engine="mysql",
                engine_version="8.0.35",
                port=3306,
                subnet_group_name="subgrp",
                security_group_ids=("sg-1",),
                publicly_accessible=False,
            )
        ],
        db_subnet_groups=[b.db_subnet_group("subgrp", "vpc-1", ("subnet-1", "subnet-2"))],
    )
    objs = normalize(snap, ctx)
    inst = objs[0]
    assert inst.network.network == "network:test-vpc"
    assert inst.network.subnets == ["subnet:test-vpc-subnet-0", "subnet:test-vpc-subnet-1"]
    assert inst.network.firewall_policies == ["firewall_policy:db-sg"]
    assert ctx.objects["firewall_policy:db-sg"].applies_to == ["database_instance:private-mysql"]


def test_read_replica_resolves_to_ir_id_of_primary():
    ctx = _ctx()
    snap = b.snapshot(
        db_instances=[
            b.db_instance("primary-db"),
            b.db_instance("replica-db", read_replica_source_id="primary-db"),
        ]
    )
    objs = normalize(snap, ctx)
    by_id = {o.id: o for o in objs}
    replica = by_id["database_instance:replica-db"]
    assert replica.role == "read_replica"
    assert replica.replica_of == "database_instance:primary-db"


def test_read_replica_with_missing_primary_is_external():
    ctx = _ctx()
    snap = b.snapshot(
        db_instances=[
            b.db_instance("replica-db", read_replica_source_id="not-in-scan"),
        ]
    )
    objs = normalize(snap, ctx)
    replica = objs[0]
    assert replica.role == "read_replica"
    assert replica.replica_of == "external:not-in-scan"
    assert "database_instance:" not in replica.replica_of


def test_multi_az_sets_high_availability():
    ctx = _ctx()
    snap = b.snapshot(db_instances=[b.db_instance("ha-db", multi_az=True)])
    objs = normalize(snap, ctx)
    assert objs[0].high_availability is True
    assert objs[0].multi_az_cluster is False


def test_multi_az_db_cluster_member_sets_multi_az_cluster():
    ctx = _ctx()
    snap = b.snapshot(
        db_instances=[b.db_instance("cluster-member", db_cluster_identifier="cluster-1")],
        db_clusters=[b.db_cluster("cluster-1", engine="postgres", members=("cluster-member",))],
    )
    objs = normalize(snap, ctx)
    assert objs[0].multi_az_cluster is True


def test_parameters_keep_user_set_values_including_formula_defaults_excluded():
    ctx = _ctx()
    snap = b.snapshot(
        db_instances=[b.db_instance("param-db", parameter_group_names=("pg1",))],
        db_parameter_groups=[b.parameter_group("pg1")],
        db_parameters={
            "pg1": [
                b.parameter("log_min_duration_statement", "1000"),
                b.parameter("shared_buffers", "{DBInstanceClassMemory/32768}"),
                b.parameter("no_value_set", None),
            ]
        },
    )
    objs = normalize(snap, ctx)
    assert objs[0].parameters == {
        "log_min_duration_statement": "1000",
        "shared_buffers": "{DBInstanceClassMemory/32768}",
    }


def test_vcpu_memory_from_instance_types():
    ctx = _ctx()
    snap = b.snapshot(
        db_instances=[b.db_instance("db1", instance_class="db.m5.2xlarge")],
        instance_types=[b.instance_type("m5.2xlarge", vcpus=8, memory_mib=32768)],
    )
    objs = normalize(snap, ctx)
    assert objs[0].compute.vcpus == 8
    assert objs[0].compute.memory_mib == 32768
    assert ctx.findings == []


def test_vcpu_memory_from_fallback_table():
    ctx = _ctx()
    snap = b.snapshot(db_instances=[b.db_instance("db1", instance_class="db.t3.micro")])
    objs = normalize(snap, ctx)
    assert objs[0].compute.vcpus == 2
    assert objs[0].compute.memory_mib == 1024
    assert ctx.findings == []


def test_vcpu_memory_unknown_gives_none_and_scan_gap_finding():
    ctx = _ctx()
    snap = b.snapshot(db_instances=[b.db_instance("db1", instance_class="db.unknown.wild")])
    objs = normalize(snap, ctx)
    assert objs[0].compute.vcpus is None
    assert objs[0].compute.memory_mib is None
    codes = [f.code for f in ctx.findings]
    assert "SCAN-DB-CLASS-UNKNOWN" in codes


def test_aurora_mariadb_oracle_produce_three_unsupported_findings_zero_ir_objects():
    ctx = _ctx()
    snap = b.snapshot(
        db_instances=[
            b.db_instance("aurora-db", engine="aurora-postgresql"),
            b.db_instance("mariadb-db", engine="mariadb"),
            b.db_instance("oracle-db", engine="oracle-se2"),
        ]
    )
    objs = normalize(snap, ctx)
    assert objs == []
    unsupported = [f for f in ctx.findings if f.level == "unsupported"]
    assert len(unsupported) == 3
    assert all(f.code == "DB-ENGINE-UNSUPPORTED" for f in unsupported)
    subjects = {f.subject for f in unsupported}
    assert subjects == {"aurora-db", "mariadb-db", "oracle-db"}


def test_unresolvable_vpc_gives_none_network_and_scan_gap_finding_document_still_validates():
    ctx = _ctx()
    snap = b.snapshot(
        db_instances=[b.db_instance("db1", subnet_group_name="subgrp")],
        db_subnet_groups=[b.db_subnet_group("subgrp", "vpc-not-scanned", ("subnet-x", "subnet-y"))],
    )
    objs = normalize(snap, ctx)
    assert objs[0].network.network is None
    assert objs[0].network.subnets == []
    codes = [f.code for f in ctx.findings]
    assert "SCAN-DB-NETWORK-UNRESOLVED" in codes
    from easymigrate.ir.base import IRDocument

    doc = IRDocument(
        source={"cloud": "aws", "account_id": "123456789012", "region": "ap-south-1"},
        objects=objs,
        findings=ctx.findings,
    )
    assert doc.objects[0].network.network is None


def test_status_creating_records_status_and_info_finding():
    ctx = _ctx()
    snap = b.snapshot(db_instances=[b.db_instance("db1", status="creating")])
    objs = normalize(snap, ctx)
    assert objs[0].status == "creating"
    info = [f for f in ctx.findings if f.code == "DB-STATUS-TRANSIENT"]
    assert len(info) == 1
    assert info[0].level == "info"


def test_missing_endpoint_falls_back_to_engine_default_port_with_scan_gap_finding():
    ctx = _ctx()
    # A custom-port instance scanned before it has an Endpoint (e.g. still "creating"): AWS
    # never reported a port, so the normalizer must not silently invent the wrong one without
    # saying so.
    snap = b.snapshot(
        db_instances=[
            b.db_instance(
                "db1", engine="postgres", port=5433, has_endpoint=False, status="creating"
            )
        ]
    )
    objs = normalize(snap, ctx)
    assert objs[0].network.port == 5432  # the engine default, not the real configured 5433
    codes = [f.code for f in ctx.findings]
    assert "SCAN-DB-PORT-UNKNOWN" in codes
    finding = next(f for f in ctx.findings if f.code == "SCAN-DB-PORT-UNKNOWN")
    assert finding.level == "scan_gap"
    assert finding.subject == objs[0].id


def test_endpoint_present_uses_its_reported_port_no_finding():
    ctx = _ctx()
    snap = b.snapshot(db_instances=[b.db_instance("db1", engine="postgres", port=5433)])
    objs = normalize(snap, ctx)
    assert objs[0].network.port == 5433
    assert [f for f in ctx.findings if f.code == "SCAN-DB-PORT-UNKNOWN"] == []


def test_subnet_group_spanning_two_vpcs_is_unmodelled_strict_raises():
    ctx = _ctx(strict=True)
    _network_objects(ctx, vpc_id="vpc-1", subnet_ids=("subnet-1",), vpc_name="vpc-one")
    _network_objects(ctx, vpc_id="vpc-2", subnet_ids=("subnet-2",), vpc_name="vpc-two")
    snap = b.snapshot(
        db_instances=[b.db_instance("db1", subnet_group_name="subgrp")],
        # DBSubnetGroup claims vpc-1, but subnet-2 (resolved via ctx.objects) actually belongs
        # to vpc-2 -> the group spans two VPCs, which is impossible in real AWS.
        db_subnet_groups=[b.db_subnet_group("subgrp", "vpc-1", ("subnet-1", "subnet-2"))],
    )
    try:
        normalize(snap, ctx)
        raised = False
    except UnmodelledData:
        raised = True
    assert raised


def test_normalizing_same_snapshot_twice_is_deterministic():
    ctx1 = _ctx()
    _network_objects(ctx1)
    ctx2 = _ctx()
    _network_objects(ctx2)
    snap = b.snapshot(
        db_instances=[
            b.db_instance("db-b", subnet_group_name="subgrp", security_group_ids=("sg-1",)),
            b.db_instance("db-a"),
        ],
        db_subnet_groups=[b.db_subnet_group("subgrp", "vpc-1", ("subnet-1", "subnet-2"))],
    )
    objs1 = normalize(snap, ctx1)
    objs2 = normalize(snap, ctx2)
    dump1 = [o.model_dump(mode="json") for o in objs1]
    dump2 = [o.model_dump(mode="json") for o in objs2]
    assert dump1 == dump2
    assert [o.id for o in objs1] == sorted(o.id for o in objs1)
