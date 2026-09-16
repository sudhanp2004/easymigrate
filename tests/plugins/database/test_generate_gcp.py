"""Generator unit tests, one per contract point (spec §6, plan 3 A1/A2)."""

from __future__ import annotations

import re

import pytest

from easymigrate.core.plugin import GenerateContext, GenerationError
from easymigrate.ir.base import IRDocument, ResolutionRef, SourceRef
from easymigrate.ir.database import DatabaseInstance, DbCompute, DbNetwork, DbStorage
from easymigrate.ir.network import Network
from easymigrate.plugins.database.generate_gcp import database_password_variables, generate


def _src(type_: str, id_: str) -> SourceRef:
    return SourceRef(cloud="aws", type=type_, id=id_, region="ap-south-1")


def _network(id_: str, name: str, **kw) -> Network:
    return Network(
        id=f"network:{id_}",
        name=name,
        source=_src("vpc", f"vpc-{id_}"),
        cidr_blocks=kw.pop("cidr_blocks", ["10.0.0.0/16"]),
        dns_hostnames=kw.pop("dns_hostnames", True),
        dns_resolution=kw.pop("dns_resolution", True),
        **kw,
    )


def _instance(
    id_: str,
    *,
    engine: str = "postgres",
    engine_version: str = "15.4",
    role: str = "primary",
    replica_of: str | None = None,
    publicly_accessible: bool = False,
    network: str | None = None,
    port: int = 5432,
    hints: dict | None = None,
    resolution: list[ResolutionRef] | None = None,
    **kw,
) -> DatabaseInstance:
    instance = DatabaseInstance(
        id=f"database_instance:{id_}",
        name=id_,
        source=_src("db", f"db-{id_}"),
        engine=engine,
        engine_version=engine_version,
        role=role,
        replica_of=replica_of,
        compute=kw.pop("compute", DbCompute(class_name="db.t3.micro", vcpus=1, memory_mib=1024)),
        storage=kw.pop("storage", DbStorage(size_gib=20, type="gp2")),
        network=DbNetwork(
            network=f"network:{network}" if network else None,
            publicly_accessible=publicly_accessible,
            port=port,
        ),
        resolution=resolution or [],
        **kw,
    )
    if hints:
        instance = instance.model_copy(update={"target_hints": hints})
    return instance


def _norm(out: str) -> str:
    """Collapse the HCL renderer's per-group column alignment so attribute assertions
    don't depend on the width of neighbouring attribute names."""
    return re.sub(r"[ \t]+=", " =", out)


def _ctx(**kw) -> GenerateContext:
    return GenerateContext(project="p", region="asia-south1", version="1.2.3", **kw)


_BASE_HINTS = {"database_version": "POSTGRES_15", "tier": "db-custom-1-3840"}


# ---- public instance ----------------------------------------------------------------


def test_public_postgres_primary_has_public_ip_and_authorized_networks():
    instance = _instance(
        "pub",
        publicly_accessible=True,
        hints={
            **_BASE_HINTS,
            "public_ip": True,
            "authorized_networks": ["10.0.0.0/24", "0.0.0.0/0"],
        },
    )
    doc = IRDocument(source={}, objects=[instance])

    out = _norm(generate(doc, _ctx()).render())

    assert "ipv4_enabled = true" in out
    assert 'ssl_mode = "ENCRYPTED_ONLY"' in out
    assert 'name = "10-0-0-0-24"' in out
    assert 'value = "10.0.0.0/24"' in out
    assert "private_network" not in out
    assert "depends_on" not in out


# ---- private instance -----------------------------------------------------------------


def test_private_mysql_primary_has_private_network_and_binary_log():
    net = _network("vpc1", "vpc-1")
    instance = _instance(
        "priv",
        engine="mysql",
        engine_version="8.0.35",
        network="vpc1",
        hints={
            "database_version": "MYSQL_8_0",
            "tier": "db-custom-2-7680",
            "private_network": "network:vpc1",
            "psa": {"range": "10.64.0.0", "prefix_length": 24},
            "pitr": {"enabled": True, "log_retention_days": 7},
        },
    )
    doc = IRDocument(source={}, objects=[net, instance])

    out = _norm(generate(doc, _ctx()).render())

    assert "ipv4_enabled = false" in out
    assert "private_network = google_compute_network.vpc1.id" in out
    assert "depends_on = [google_service_networking_connection.vpc1_psa]" in out
    assert "binary_log_enabled" in out
    assert "point_in_time_recovery_enabled" not in out


# ---- PSA dedup / conflicts ------------------------------------------------------------


def _private_instance(id_, network_id, psa, **kw):
    return _instance(
        id_,
        network=network_id,
        hints={
            **_BASE_HINTS,
            "private_network": f"network:{network_id}",
            "psa": psa,
            **kw.pop("hints", {}),
        },
        **kw,
    )


def test_psa_emitted_once_for_two_instances_in_one_network():
    net = _network("vpc1", "vpc-1")
    psa = {"range": "10.64.0.0", "prefix_length": 24}
    i1 = _private_instance("a", "vpc1", psa)
    i2 = _private_instance("b", "vpc1", psa)
    doc = IRDocument(source={}, objects=[net, i1, i2])

    out = generate(doc, _ctx())
    rendered = out.render()

    assert rendered.count('resource "google_compute_global_address" "vpc1_psa"') == 1
    assert rendered.count('resource "google_service_networking_connection" "vpc1_psa"') == 1


def test_psa_emitted_per_network_for_two_networks():
    net1 = _network("vpc1", "vpc-1")
    net2 = _network("vpc2", "vpc-2", cidr_blocks=["10.1.0.0/16"])
    psa1 = {"range": "10.64.0.0", "prefix_length": 24}
    psa2 = {"range": "10.64.1.0", "prefix_length": 24}
    i1 = _private_instance("a", "vpc1", psa1)
    i2 = _private_instance("b", "vpc2", psa2)
    doc = IRDocument(source={}, objects=[net1, net2, i1, i2])

    rendered = generate(doc, _ctx()).render()

    assert 'resource "google_compute_global_address" "vpc1_psa"' in rendered
    assert 'resource "google_compute_global_address" "vpc2_psa"' in rendered


def test_conflicting_psa_ranges_in_one_network_raises_naming_both():
    net = _network("vpc1", "vpc-1")
    i1 = _private_instance("a", "vpc1", {"range": "10.64.0.0", "prefix_length": 24})
    i2 = _private_instance("b", "vpc1", {"range": "10.64.1.0", "prefix_length": 24})
    doc = IRDocument(source={}, objects=[net, i1, i2])

    with pytest.raises(GenerationError) as excinfo:
        generate(doc, _ctx())

    assert "database_instance:a" in str(excinfo.value)
    assert "database_instance:b" in str(excinfo.value)


# ---- replica ----------------------------------------------------------------------


def test_replica_has_master_instance_no_backup_no_user_no_variable():
    primary = _instance("primary", hints=_BASE_HINTS | {"public_ip": True})
    replica = _instance(
        "replica",
        role="read_replica",
        replica_of="database_instance:primary",
        publicly_accessible=True,
        hints=_BASE_HINTS | {"public_ip": True, "master_instance": "database_instance:primary"},
    )
    doc = IRDocument(source={}, objects=[primary, replica])

    out = generate(doc, _ctx()).render()

    assert (
        "master_instance_name = google_sql_database_instance.replica.name" not in out
    )  # replica references the primary, not itself
    assert "master_instance_name = google_sql_database_instance.primary.name" in out
    assert 'resource "google_sql_user" "replica"' not in out
    assert 'variable "db_password_replica"' not in out
    assert 'resource "google_sql_user" "primary"' in out
    assert 'variable "db_password_primary"' in out
    # the replica's own settings block has no backup_configuration
    replica_block = out.split('resource "google_sql_database_instance" "replica"')[1]
    replica_block = replica_block.split("\n\nresource ")[0]
    assert "backup_configuration" not in replica_block


def test_replica_of_external_primary_raises_generation_error():
    replica = _instance(
        "orphan",
        role="read_replica",
        replica_of="external:rds-somewhere",
        publicly_accessible=True,
        hints=_BASE_HINTS | {"public_ip": True},
    )
    doc = IRDocument(source={}, objects=[replica])

    with pytest.raises(GenerationError, match="database_instance:orphan"):
        generate(doc, _ctx())


# ---- database_flags -----------------------------------------------------------------


def test_database_flags_rendered_sorted():
    instance = _instance(
        "flags",
        publicly_accessible=True,
        hints=_BASE_HINTS | {"public_ip": True, "database_flags": {"zeta": "1", "alpha": "on"}},
    )
    doc = IRDocument(source={}, objects=[instance])

    out = generate(doc, _ctx()).render()

    alpha_idx = out.index('name  = "alpha"')
    zeta_idx = out.index('name  = "zeta"')
    assert alpha_idx < zeta_idx


def test_empty_database_flags_emits_no_block():
    instance = _instance(
        "noflags", publicly_accessible=True, hints=_BASE_HINTS | {"public_ip": True}
    )
    doc = IRDocument(source={}, objects=[instance])

    out = generate(doc, _ctx()).render()

    assert "database_flags" not in out


# ---- disk_autoresize_limit ------------------------------------------------------------


def test_disk_autoresize_limit_omitted_when_autoresize_off():
    instance = _instance(
        "noresize",
        publicly_accessible=True,
        hints=_BASE_HINTS | {"public_ip": True, "disk_autoresize": False},
    )
    doc = IRDocument(source={}, objects=[instance])

    out = _norm(generate(doc, _ctx()).render())

    assert "disk_autoresize = false" in out
    assert "disk_autoresize_limit" not in out


def test_disk_autoresize_limit_present_when_autoresize_on():
    instance = _instance(
        "resize",
        publicly_accessible=True,
        hints=_BASE_HINTS
        | {"public_ip": True, "disk_autoresize": True, "disk_autoresize_limit": 500},
    )
    doc = IRDocument(source={}, objects=[instance])

    out = _norm(generate(doc, _ctx()).render())

    assert "disk_autoresize_limit = 500" in out


# ---- excluded objects --------------------------------------------------------------


def test_excluded_instance_produces_nothing():
    instance = _instance(
        "excl", publicly_accessible=True, hints=_BASE_HINTS | {"public_ip": True}
    ).model_copy(update={"excluded": "manual review"})
    doc = IRDocument(source={}, objects=[instance])

    out = generate(doc, _ctx()).render()

    assert out.strip() == generate(IRDocument(source={}, objects=[]), _ctx()).render().strip()


def test_excluded_replica_primary_raises_generation_error():
    primary = _instance(
        "excl_primary", publicly_accessible=True, hints=_BASE_HINTS | {"public_ip": True}
    ).model_copy(update={"excluded": "manual review"})
    replica = _instance(
        "child",
        role="read_replica",
        replica_of="database_instance:excl_primary",
        publicly_accessible=True,
        hints=_BASE_HINTS
        | {"public_ip": True, "master_instance": "database_instance:excl_primary"},
    )
    doc = IRDocument(source={}, objects=[primary, replica])

    with pytest.raises(GenerationError, match="database_instance:child"):
        generate(doc, _ctx())


# ---- GenerationError cases -----------------------------------------------------------


def test_missing_database_version_hint_raises():
    instance = _instance(
        "novers", publicly_accessible=True, hints={"tier": "db-custom-1-3840", "public_ip": True}
    )
    doc = IRDocument(source={}, objects=[instance])

    with pytest.raises(GenerationError, match="database_instance:novers"):
        generate(doc, _ctx())


def test_missing_tier_hint_raises():
    instance = _instance(
        "notier",
        publicly_accessible=True,
        hints={"database_version": "POSTGRES_15", "public_ip": True},
    )
    doc = IRDocument(source={}, objects=[instance])

    with pytest.raises(GenerationError, match="database_instance:notier"):
        generate(doc, _ctx())


def test_private_network_naming_missing_network_raises():
    instance = _instance(
        "orphan_net",
        hints=_BASE_HINTS | {"private_network": "network:does-not-exist"},
    )
    doc = IRDocument(source={}, objects=[instance])

    with pytest.raises(GenerationError, match="database_instance:orphan_net"):
        generate(doc, _ctx())


def test_neither_public_nor_private_network_hint_raises():
    instance = _instance("neither", hints=_BASE_HINTS)
    doc = IRDocument(source={}, objects=[instance])

    with pytest.raises(GenerationError, match="database_instance:neither"):
        generate(doc, _ctx())


def test_nonempty_parameters_without_flags_hint_raises():
    instance = _instance(
        "params",
        publicly_accessible=True,
        hints=_BASE_HINTS | {"public_ip": True},
        parameters={"max_connections": "100"},
    )
    doc = IRDocument(source={}, objects=[instance])

    with pytest.raises(GenerationError, match="database_instance:params"):
        generate(doc, _ctx())


def test_nonempty_parameters_with_dropped_parameters_hint_only_is_ok():
    instance = _instance(
        "params2",
        publicly_accessible=True,
        hints=_BASE_HINTS | {"public_ip": True, "dropped_parameters": {"foo": "bar"}},
        parameters={"foo": "bar"},
    )
    doc = IRDocument(source={}, objects=[instance])

    # should not raise
    generate(doc, _ctx())


# ---- comment headers ------------------------------------------------------------------


def test_comment_header_has_source_arn_and_resolution_decided_by():
    instance = _instance(
        "header",
        publicly_accessible=True,
        hints=_BASE_HINTS | {"public_ip": True},
        resolution=[ResolutionRef(key="engine_version", strategy="nearest_major")],
    )
    doc = IRDocument(source={}, objects=[instance])

    out = generate(doc, _ctx(decided_by={"engine_version": "user"})).render()

    assert "from AWS db db-header (header)" in out
    assert "decision engine_version → nearest_major (user)" in out
    assert "instance class: db.t3.micro" in out


# ---- database_password_variables -----------------------------------------------------


def test_database_password_variables_maps_primaries_only():
    primary = _instance("pw", publicly_accessible=True, hints=_BASE_HINTS | {"public_ip": True})
    replica = _instance(
        "pw_replica",
        role="read_replica",
        replica_of="database_instance:pw",
        publicly_accessible=True,
        hints=_BASE_HINTS | {"public_ip": True, "master_instance": "database_instance:pw"},
    )
    doc = IRDocument(source={}, objects=[primary, replica])

    variables = database_password_variables(doc)

    assert variables == {"database_instance:pw": "db_password_pw"}


def test_generator_variable_names_match_database_password_variables():
    instance = _instance("match", publicly_accessible=True, hints=_BASE_HINTS | {"public_ip": True})
    doc = IRDocument(source={}, objects=[instance])

    variables = database_password_variables(doc)
    out = generate(doc, _ctx()).render()

    for var_name in variables.values():
        assert f'variable "{var_name}"' in out
        assert f"var.{var_name}" in out
