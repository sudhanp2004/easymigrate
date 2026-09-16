"""Build the nine database golden fixtures directly from the test builders (spec 2 plan 1 §Tasks).

    uv run python scripts/make_database_fixtures.py

Unlike the network fixtures, these are not anonymized moto captures: RDS/VPC identifiers here
are plain, readable strings we chose ourselves (not AWS-random ids), so there is nothing to
anonymize. Each case builds `network.*` files (flattened lists, network collector shape) and
`database.*` files (raw-boto3 `{"<Key>": [...]}` shape, database collector shape) and writes
them straight to `tests/plugins/database/fixtures/<case>/snapshot/`.

Then run `uv run pytest --update-golden tests/plugins/database/test_golden.py` and review the
expected_ir.json diffs."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.plugins.database import aws_builders as db  # noqa: E402
from tests.plugins.network import aws_builders as net  # noqa: E402

FIXTURES = ROOT / "tests" / "plugins" / "database" / "fixtures"

Files = dict[str, Any]


def _site(
    vpc_id: str,
    cidr: str,
    vpc_name: str,
    subnet_specs: list[tuple[str, str, str]],  # (subnet_id, cidr, name)
    sg_specs: list[tuple[str, str, list[dict[str, Any]]]],  # (sg_id, name, ingress permissions)
) -> Files:
    """A VPC + subnets (+ a main route table) + security groups, network-collector shaped."""
    subnet_ids = [sid for sid, _, _ in subnet_specs]
    return {
        "network.vpcs": [net.vpc(vpc_id, cidr, name=vpc_name)],
        "network.subnets": [
            net.subnet(sid, vpc_id, scidr, name=sname) for sid, scidr, sname in subnet_specs
        ],
        "network.vpc_attributes": {vpc_id: {"enableDnsHostnames": True, "enableDnsSupport": True}},
        "network.route_tables": (
            [net.route_table(f"rtb-{vpc_id}", vpc_id, subnets=tuple(subnet_ids), main=True)]
            if subnet_ids
            else []
        ),
        "network.security_groups": [
            net.security_group(sg_id, vpc_id, sg_name, ingress=ingress, name=sg_name)
            for sg_id, sg_name, ingress in sg_specs
        ],
    }


def _default_group(family: str) -> tuple[str, dict[str, Any], dict[str, list]]:
    """A real RDS instance always has a parameter group attached — even an untouched one gets
    `default.<family>`. Returns (group name, its db_parameter_groups entry, its db_parameters
    entry). An unmodified default group has no user-set parameters, so Source=user comes back
    empty, exactly like the real collector's per-instance query would."""
    name = f"default.{family}"
    return name, db.parameter_group(name, family), {name: []}


def _db_files(
    *,
    instances: list[dict[str, Any]] = (),
    subnet_groups: list[dict[str, Any]] = (),
    parameter_groups: list[dict[str, Any]] = (),
    parameters: dict[str, list[dict[str, Any]]] | None = None,
    clusters: list[dict[str, Any]] = (),
    instance_types: list[dict[str, Any]] = (),
) -> Files:
    return {
        "database.db_instances": {"DBInstances": list(instances)},
        "database.db_subnet_groups": {"DBSubnetGroups": list(subnet_groups)},
        "database.db_parameter_groups": {"DBParameterGroups": list(parameter_groups)},
        "database.db_parameters": dict(parameters or {}),
        "database.db_clusters": {"DBClusters": list(clusters)},
        "database.instance_types": {"InstanceTypes": list(instance_types)},
    }


def public_postgres() -> tuple[Files, Files]:
    """The testbed instance: public Postgres, db.t3.micro, 20 GiB, publicly_accessible."""
    network = _site(
        "vpc-1",
        "10.0.0.0/16",
        "migration-test-vpc",
        [
            ("subnet-1", "10.0.1.0/24", "migration-test-subnet-1"),
            ("subnet-2", "10.0.2.0/24", "migration-test-subnet-2"),
        ],
        [
            (
                "sg-1",
                "public-db-sg",
                [net.permission("tcp", 5432, 5432, cidrs=("0.0.0.0/0",), description="postgres")],
            )
        ],
    )
    group_name, group_entry, params_entry = _default_group("postgres15")
    database = _db_files(
        instances=[
            db.db_instance(
                "public-postgres-db",
                name="public-postgres-db",
                engine="postgres",
                engine_version="15.5",
                instance_class="db.t3.micro",
                allocated_storage=20,
                storage_type="gp2",
                subnet_group_name="public-postgres-subgrp",
                security_group_ids=("sg-1",),
                publicly_accessible=True,
                port=5432,
                backup_retention=7,
                backup_window="03:00-04:00",
                maintenance_window="sun:04:00-sun:05:00",
                db_name="app",
                log_exports=("postgresql",),
                parameter_group_names=(group_name,),
            )
        ],
        subnet_groups=[
            db.db_subnet_group("public-postgres-subgrp", "vpc-1", ("subnet-1", "subnet-2"))
        ],
        parameter_groups=[group_entry],
        parameters=params_entry,
        instance_types=[db.instance_type("t3.micro", vcpus=2, memory_mib=1024)],
    )
    return network, database


def private_mysql_multiaz() -> tuple[Files, Files]:
    network = _site(
        "vpc-1",
        "10.1.0.0/16",
        "app-vpc",
        [("subnet-1", "10.1.1.0/24", "private-a"), ("subnet-2", "10.1.2.0/24", "private-b")],
        [
            (
                "sg-1",
                "mysql-sg",
                [net.permission("tcp", 3306, 3306, cidrs=("10.1.0.0/16",), description="mysql")],
            )
        ],
    )
    group_name, group_entry, params_entry = _default_group("mysql8.0")
    database = _db_files(
        instances=[
            db.db_instance(
                "private-mysql-db",
                name="private-mysql-db",
                engine="mysql",
                engine_version="8.0.35",
                instance_class="db.m5.large",
                allocated_storage=100,
                storage_type="gp3",
                iops=3000,
                storage_throughput=250,
                storage_encrypted=True,
                subnet_group_name="mysql-subgrp",
                security_group_ids=("sg-1",),
                publicly_accessible=False,
                port=3306,
                multi_az=True,
                deletion_protection=True,
                master_username="admin",
                parameter_group_names=(group_name,),
            )
        ],
        subnet_groups=[db.db_subnet_group("mysql-subgrp", "vpc-1", ("subnet-1", "subnet-2"))],
        parameter_groups=[group_entry],
        parameters=params_entry,
        instance_types=[db.instance_type("m5.large", vcpus=2, memory_mib=8192)],
    )
    return network, database


def read_replica() -> tuple[Files, Files]:
    network = _site(
        "vpc-1",
        "10.2.0.0/16",
        "orders-vpc",
        [("subnet-1", "10.2.1.0/24", "a"), ("subnet-2", "10.2.2.0/24", "b")],
        [
            (
                "sg-1",
                "orders-sg",
                [net.permission("tcp", 5432, 5432, cidrs=("10.2.0.0/16",))],
            )
        ],
    )
    group_name, group_entry, params_entry = _default_group("postgres15")
    database = _db_files(
        instances=[
            db.db_instance(
                "orders-db",
                name="orders-db",
                engine="postgres",
                engine_version="15.5",
                instance_class="db.t3.medium",
                allocated_storage=50,
                subnet_group_name="orders-subgrp",
                security_group_ids=("sg-1",),
                parameter_group_names=(group_name,),
            ),
            db.db_instance(
                "orders-db-replica",
                name="orders-db-replica",
                engine="postgres",
                engine_version="15.5",
                instance_class="db.t3.medium",
                allocated_storage=50,
                subnet_group_name="orders-subgrp",
                security_group_ids=("sg-1",),
                read_replica_source_id="orders-db",
                parameter_group_names=(group_name,),
            ),
        ],
        subnet_groups=[db.db_subnet_group("orders-subgrp", "vpc-1", ("subnet-1", "subnet-2"))],
        parameter_groups=[group_entry],
        parameters=params_entry,
        instance_types=[db.instance_type("t3.medium", vcpus=2, memory_mib=4096)],
    )
    return network, database


def param_flags() -> tuple[Files, Files]:
    network = _site(
        "vpc-1",
        "10.3.0.0/16",
        "flags-vpc",
        [("subnet-1", "10.3.1.0/24", "a"), ("subnet-2", "10.3.2.0/24", "b")],
        [("sg-1", "flags-sg", [net.permission("tcp", 5432, 5432, cidrs=("10.3.0.0/16",))])],
    )
    database = _db_files(
        instances=[
            db.db_instance(
                "flagged-db",
                name="flagged-db",
                engine="postgres",
                engine_version="15.5",
                subnet_group_name="flags-subgrp",
                security_group_ids=("sg-1",),
                parameter_group_names=("flagged-db-params",),
            )
        ],
        subnet_groups=[db.db_subnet_group("flags-subgrp", "vpc-1", ("subnet-1", "subnet-2"))],
        parameter_groups=[db.parameter_group("flagged-db-params")],
        parameters={
            "flagged-db-params": [
                # A flag DB-PARAM-FLAGS will later translate to a Cloud SQL database_flag.
                db.parameter("log_min_duration_statement", "1000"),
                # An RDS-only knob with no Cloud SQL equivalent; captured verbatim regardless -
                # judging it is DB-PARAM-FLAGS's job (plan 2), not the normalizer's.
                db.parameter("rds.force_ssl", "1"),
                # An AWS formula string, kept as-is.
                db.parameter("shared_buffers", "{DBInstanceClassMemory/32768}"),
            ]
        },
        instance_types=[db.instance_type("t3.micro", vcpus=2, memory_mib=1024)],
    )
    return network, database


def unsupported_engines() -> tuple[Files, Files]:
    # No network resources: neither instance becomes an IR object, so there is nothing to
    # resolve a VPC/subnet/security group for.
    network: Files = {}
    aurora_group, aurora_entry, aurora_params = _default_group("aurora-postgresql15")
    oracle_group, oracle_entry, oracle_params = _default_group("oracle-se2-19")
    database = _db_files(
        instances=[
            db.db_instance(
                "aurora-instance",
                engine="aurora-postgresql",
                engine_version="15.4",
                parameter_group_names=(aurora_group,),
            ),
            db.db_instance(
                "oracle-instance",
                engine="oracle-se2",
                engine_version="19.0.0.0.ru-2023-10",
                parameter_group_names=(oracle_group,),
            ),
        ],
        clusters=[
            db.db_cluster(
                "aurora-cluster", engine="aurora-postgresql", members=("aurora-instance",)
            )
        ],
        parameter_groups=[aurora_entry, oracle_entry],
        parameters={**aurora_params, **oracle_params},
    )
    return network, database


def custom_port() -> tuple[Files, Files]:
    network = _site(
        "vpc-1",
        "10.4.0.0/16",
        "custom-port-vpc",
        [("subnet-1", "10.4.1.0/24", "a"), ("subnet-2", "10.4.2.0/24", "b")],
        [("sg-1", "custom-port-sg", [net.permission("tcp", 5433, 5433, cidrs=("10.4.0.0/16",))])],
    )
    group_name, group_entry, params_entry = _default_group("postgres15")
    database = _db_files(
        instances=[
            db.db_instance(
                "custom-port-db",
                name="custom-port-db",
                engine="postgres",
                engine_version="15.5",
                subnet_group_name="custom-port-subgrp",
                security_group_ids=("sg-1",),
                port=5433,
                parameter_group_names=(group_name,),
            )
        ],
        subnet_groups=[db.db_subnet_group("custom-port-subgrp", "vpc-1", ("subnet-1", "subnet-2"))],
        parameter_groups=[group_entry],
        parameters=params_entry,
        instance_types=[db.instance_type("t3.micro", vcpus=2, memory_mib=1024)],
    )
    return network, database


def old_engine_version() -> tuple[Files, Files]:
    network = _site(
        "vpc-1",
        "10.5.0.0/16",
        "legacy-vpc",
        [("subnet-1", "10.5.1.0/24", "a"), ("subnet-2", "10.5.2.0/24", "b")],
        [("sg-1", "legacy-sg", [net.permission("tcp", 5432, 5432, cidrs=("10.5.0.0/16",))])],
    )
    group_name, group_entry, params_entry = _default_group("postgres9.6")
    database = _db_files(
        instances=[
            db.db_instance(
                "legacy-db",
                name="legacy-db",
                engine="postgres",
                engine_version="9.6.24",
                instance_class="db.t3.small",
                subnet_group_name="legacy-subgrp",
                security_group_ids=("sg-1",),
                parameter_group_names=(group_name,),
            )
        ],
        subnet_groups=[db.db_subnet_group("legacy-subgrp", "vpc-1", ("subnet-1", "subnet-2"))],
        parameter_groups=[group_entry],
        parameters=params_entry,
        instance_types=[db.instance_type("t3.small", vcpus=2, memory_mib=2048)],
    )
    return network, database


def psa_range_conflict() -> tuple[Files, Files]:
    # VPC CIDR chosen to collide with the default Private Services Access allocation range
    # plan 2's DB-PRIVATE-IP rule will try first, forcing it to pick a different /24.
    network = _site(
        "vpc-1",
        "10.64.0.0/16",
        "psa-conflict-vpc",
        [("subnet-1", "10.64.1.0/24", "a"), ("subnet-2", "10.64.2.0/24", "b")],
        [("sg-1", "psa-conflict-sg", [net.permission("tcp", 5432, 5432, cidrs=("10.64.0.0/16",))])],
    )
    group_name, group_entry, params_entry = _default_group("postgres15")
    database = _db_files(
        instances=[
            db.db_instance(
                "conflict-db",
                name="conflict-db",
                engine="postgres",
                engine_version="15.5",
                subnet_group_name="conflict-subgrp",
                security_group_ids=("sg-1",),
                publicly_accessible=False,
                parameter_group_names=(group_name,),
            )
        ],
        subnet_groups=[db.db_subnet_group("conflict-subgrp", "vpc-1", ("subnet-1", "subnet-2"))],
        parameter_groups=[group_entry],
        parameters=params_entry,
        instance_types=[db.instance_type("t3.micro", vcpus=2, memory_mib=1024)],
    )
    return network, database


def no_backups() -> tuple[Files, Files]:
    network = _site(
        "vpc-1",
        "10.6.0.0/16",
        "no-backups-vpc",
        [("subnet-1", "10.6.1.0/24", "a"), ("subnet-2", "10.6.2.0/24", "b")],
        [("sg-1", "no-backups-sg", [net.permission("tcp", 5432, 5432, cidrs=("10.6.0.0/16",))])],
    )
    group_name, group_entry, params_entry = _default_group("postgres15")
    database = _db_files(
        instances=[
            db.db_instance(
                "no-backup-db",
                name="no-backup-db",
                engine="postgres",
                engine_version="15.5",
                subnet_group_name="no-backups-subgrp",
                security_group_ids=("sg-1",),
                publicly_accessible=False,
                backup_retention=0,
                parameter_group_names=(group_name,),
            )
        ],
        subnet_groups=[db.db_subnet_group("no-backups-subgrp", "vpc-1", ("subnet-1", "subnet-2"))],
        parameter_groups=[group_entry],
        parameters=params_entry,
        instance_types=[db.instance_type("t3.micro", vcpus=2, memory_mib=1024)],
    )
    return network, database


CASES: dict[str, Callable[[], tuple[Files, Files]]] = {
    "public_postgres": public_postgres,
    "private_mysql_multiaz": private_mysql_multiaz,
    "read_replica": read_replica,
    "param_flags": param_flags,
    "unsupported_engines": unsupported_engines,
    "custom_port": custom_port,
    "old_engine_version": old_engine_version,
    "psa_range_conflict": psa_range_conflict,
    "no_backups": no_backups,
}


def main() -> None:
    for case, build in CASES.items():
        network_files, database_files = build()
        target = FIXTURES / case / "snapshot"
        target.mkdir(parents=True, exist_ok=True)
        for old in target.glob("*.json"):
            old.unlink()
        for name, data in {**network_files, **database_files}.items():
            (target / f"{name}.json").write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        print(f"wrote {target.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
