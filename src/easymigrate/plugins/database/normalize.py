"""Database plugin normalizer entry point: snapshot → database_instance IR objects (spec §4.2).

Faithful capture only: nothing here decides whether GCP can reproduce what it records. VPC,
subnet and security-group native ids resolve to IR ids through `ctx.objects`, which the network
plugin has already populated by the time this runs (registry order is load-bearing, spec §3)."""

from __future__ import annotations

from typing import Any

from easymigrate.core.plugin import NormalizeContext, Snapshot
from easymigrate.ir.base import Finding, IntentObject, SourceRef
from easymigrate.ir.database import (
    DatabaseInstance,
    DbBackup,
    DbCompute,
    DbMaintenance,
    DbNetwork,
    DbStorage,
)
from easymigrate.ir.naming import allocate_ids, clean_labels
from easymigrate.plugins.database.aws_values import (
    DEFAULT_PORT,
    FALLBACK_INSTANCE_TYPES,
    SUPPORTED_ENGINES,
    UNSUPPORTED_ENGINE_REASONS,
)

# Engines a `DBClusterIdentifier` can name that mean "Multi-AZ DB cluster", not Aurora.
_MULTIAZ_CLUSTER_ENGINES = frozenset({"postgres", "mysql"})


def _tags(resource: dict[str, Any]) -> dict[str, str]:
    return {tag["Key"]: tag["Value"] for tag in resource.get("TagList") or []}


def _name_tag(resource: dict[str, Any]) -> str | None:
    return _tags(resource).get("Name", "").strip() or None


def _source(ctx: NormalizeContext, type_: str, native_id: str) -> SourceRef:
    return SourceRef(cloud="aws", type=type_, id=native_id, region=ctx.region)


def _unsupported_reason(engine: str) -> str | None:
    for prefix, reason in UNSUPPORTED_ENGINE_REASONS.items():
        if engine.startswith(prefix):
            return reason
    return None


def normalize(snapshot: Snapshot, ctx: NormalizeContext) -> list[IntentObject]:
    instances_raw = (snapshot.get("database.db_instances") or {}).get("DBInstances", [])
    subnet_groups_raw = (snapshot.get("database.db_subnet_groups") or {}).get("DBSubnetGroups", [])
    db_parameters = snapshot.get("database.db_parameters") or {}
    clusters_raw = (snapshot.get("database.db_clusters") or {}).get("DBClusters", [])
    instance_types_raw = (snapshot.get("database.instance_types") or {}).get("InstanceTypes", [])

    subnet_groups = {g["DBSubnetGroupName"]: g for g in subnet_groups_raw}
    clusters = {c["DBClusterIdentifier"]: c for c in clusters_raw}
    instance_type_sizes = {
        it["InstanceType"]: (it["VCpuInfo"]["DefaultVCpus"], it["MemoryInfo"]["SizeInMiB"])
        for it in instance_types_raw
    }

    _report_unsupported_clusters(clusters_raw, ctx)

    supported: list[dict[str, Any]] = []
    for inst in instances_raw:
        if inst["Engine"] in SUPPORTED_ENGINES:
            supported.append(inst)
        else:
            _report_unsupported_instance(inst, ctx)

    ids = allocate_ids(
        "database_instance",
        [(_name_tag(inst), inst["DBInstanceIdentifier"]) for inst in supported],
    )
    identifier_to_ir = {
        inst["DBInstanceIdentifier"]: ids[inst["DBInstanceIdentifier"]] for inst in supported
    }

    network_lookup, subnet_lookup, policy_lookup = _topology_lookups(ctx)

    objects = [
        _build_instance(
            inst,
            ids[inst["DBInstanceIdentifier"]],
            subnet_groups,
            db_parameters,
            clusters,
            instance_type_sizes,
            identifier_to_ir,
            network_lookup,
            subnet_lookup,
            policy_lookup,
            ctx,
        )
        for inst in supported
    ]
    return sorted(objects, key=lambda obj: obj.id)


def _topology_lookups(
    ctx: NormalizeContext,
) -> tuple[dict[str, str], dict[str, str], dict[str, IntentObject]]:
    network_lookup: dict[str, str] = {}
    subnet_lookup: dict[str, str] = {}
    policy_lookup: dict[str, IntentObject] = {}
    for obj in ctx.objects.values():
        if obj.kind == "network":
            network_lookup[obj.source.id] = obj.id
        elif obj.kind == "subnet":
            subnet_lookup[obj.source.id] = obj.id
        elif obj.kind == "firewall_policy":
            policy_lookup[obj.source.id] = obj
    return network_lookup, subnet_lookup, policy_lookup


def _report_unsupported_clusters(clusters_raw: list[dict[str, Any]], ctx: NormalizeContext) -> None:
    for cluster in clusters_raw:
        reason = _unsupported_reason(cluster["Engine"])
        if reason is None:
            continue
        ctx.findings.append(
            Finding(
                level="unsupported",
                code="DB-ENGINE-UNSUPPORTED",
                message=(
                    f"Cluster {cluster['DBClusterIdentifier']!r} uses engine "
                    f"{cluster['Engine']!r}, which EasyMigrate does not yet migrate: {reason}"
                ),
                subject=cluster["DBClusterIdentifier"],
            )
        )


def _report_unsupported_instance(inst: dict[str, Any], ctx: NormalizeContext) -> None:
    engine = inst["Engine"]
    reason = _unsupported_reason(engine) or "this engine is not yet supported."
    ctx.findings.append(
        Finding(
            level="unsupported",
            code="DB-ENGINE-UNSUPPORTED",
            message=(
                f"{inst['DBInstanceIdentifier']!r} uses engine {engine!r}, which EasyMigrate "
                f"does not yet migrate: {reason}"
            ),
            subject=inst["DBInstanceIdentifier"],
        )
    )


def _role(inst: dict[str, Any], identifier_to_ir: dict[str, str]) -> tuple[str, str | None]:
    source_id = inst.get("ReadReplicaSourceDBInstanceIdentifier")
    if source_id is None:
        return "primary", None
    replica_of = identifier_to_ir.get(source_id) or f"external:{source_id}"
    return "read_replica", replica_of


def _is_multiaz_cluster(inst: dict[str, Any], clusters: dict[str, Any]) -> bool:
    cluster_id = inst.get("DBClusterIdentifier")
    if not cluster_id:
        return False
    cluster = clusters.get(cluster_id)
    return cluster is not None and cluster.get("Engine") in _MULTIAZ_CLUSTER_ENGINES


def _compute(
    inst: dict[str, Any],
    instance_type_sizes: dict[str, tuple[int, int]],
    ir_id: str,
    ctx: NormalizeContext,
) -> DbCompute:
    class_name = inst["DBInstanceClass"]
    stripped = class_name.removeprefix("db.")
    sizing = instance_type_sizes.get(stripped) or FALLBACK_INSTANCE_TYPES.get(stripped)
    if sizing is None:
        ctx.findings.append(
            Finding(
                level="scan_gap",
                code="SCAN-DB-CLASS-UNKNOWN",
                message=(
                    f"No vCPU/memory figures are known for instance class {class_name!r}; "
                    "recorded with vcpus and memory_mib unset."
                ),
                subject=ir_id,
            )
        )
        return DbCompute(class_name=class_name, vcpus=None, memory_mib=None)
    vcpus, memory_mib = sizing
    return DbCompute(class_name=class_name, vcpus=vcpus, memory_mib=memory_mib)


def _storage(inst: dict[str, Any]) -> DbStorage:
    return DbStorage(
        size_gib=inst["AllocatedStorage"],
        max_size_gib=inst.get("MaxAllocatedStorage"),
        type=inst.get("StorageType", "standard"),
        # Real RDS reports Iops/StorageThroughput as 0 on storage types that have no
        # provisioned value (gp2, standard), rather than omitting them. 0 means "not
        # applicable", so record it as absent — the IR only allows these on gp3/io1/io2.
        iops=inst.get("Iops") or None,
        throughput_mibps=inst.get("StorageThroughput") or None,
        encrypted=bool(inst.get("StorageEncrypted")),
        kms_key=inst.get("KmsKeyId"),
    )


def _resolve_firewall_policies(
    inst: dict[str, Any], ir_id: str, policy_lookup: dict[str, IntentObject]
) -> list[str]:
    resolved: set[str] = set()
    for sg in inst.get("VpcSecurityGroups") or []:
        policy = policy_lookup.get(sg.get("VpcSecurityGroupId"))
        if policy is None:
            continue
        resolved.add(policy.id)
    return sorted(resolved)


def _network(
    inst: dict[str, Any],
    ir_id: str,
    subnet_groups: dict[str, Any],
    network_lookup: dict[str, str],
    subnet_lookup: dict[str, str],
    policy_lookup: dict[str, IntentObject],
    ctx: NormalizeContext,
) -> DbNetwork:
    identifier = inst["DBInstanceIdentifier"]
    group_name = (inst.get("DBSubnetGroup") or {}).get("DBSubnetGroupName")
    group = subnet_groups.get(group_name) if group_name else None

    network_ir: str | None = None
    subnet_irs: list[str] = []
    unresolved: list[str] = []

    if group is not None:
        vpc_id = group.get("VpcId")
        network_ir = network_lookup.get(vpc_id)
        if network_ir is None and vpc_id is not None:
            unresolved.append(vpc_id)

        member_networks: set[str] = set()
        for member in group.get("Subnets") or []:
            subnet_native_id = member["SubnetIdentifier"]
            subnet_ir = subnet_lookup.get(subnet_native_id)
            if subnet_ir is None:
                unresolved.append(subnet_native_id)
            else:
                subnet_irs.append(subnet_ir)
                member_networks.add(ctx.objects[subnet_ir].network)
        if len(member_networks) > 1:
            ctx.unmodelled(
                f"DB subnet group {group_name!r} spans multiple VPCs",
                subject=group_name,
            )

    if unresolved:
        ctx.findings.append(
            Finding(
                level="scan_gap",
                code="SCAN-DB-NETWORK-UNRESOLVED",
                message=(
                    f"{identifier}: could not resolve {', '.join(sorted(unresolved))} to a "
                    "scanned network object; it likely lives outside the scanned region."
                ),
                subject=ir_id,
            )
        )

    endpoint_port = (inst.get("Endpoint") or {}).get("Port")
    if endpoint_port is not None:
        port = endpoint_port
    else:
        # No endpoint yet (typically an instance still `creating`): the IR model requires a
        # port, but AWS has not reported one. Assume the engine default and say so, rather
        # than silently inventing a value the scan never saw.
        port = DEFAULT_PORT[inst["Engine"]]
        ctx.findings.append(
            Finding(
                level="scan_gap",
                code="SCAN-DB-PORT-UNKNOWN",
                message=(
                    f"{identifier}: no endpoint has been assigned yet, so the real port is "
                    f"unknown; recorded as the engine default {port}, which may be wrong."
                ),
                subject=ir_id,
            )
        )

    return DbNetwork(
        network=network_ir,
        subnets=sorted(set(subnet_irs)),
        publicly_accessible=bool(inst.get("PubliclyAccessible")),
        port=port,
        firewall_policies=_resolve_firewall_policies(inst, ir_id, policy_lookup),
    )


def _parameters(inst: dict[str, Any], db_parameters: dict[str, Any]) -> dict[str, str]:
    groups = inst.get("DBParameterGroups") or []
    if not groups:
        return {}
    group_name = groups[0].get("DBParameterGroupName")
    result: dict[str, str] = {}
    for parameter in db_parameters.get(group_name) or []:
        value = parameter.get("ParameterValue")
        if value is None:
            continue
        result[parameter["ParameterName"]] = value
    return dict(sorted(result.items()))


def _build_instance(
    inst: dict[str, Any],
    ir_id: str,
    subnet_groups: dict[str, Any],
    db_parameters: dict[str, Any],
    clusters: dict[str, Any],
    instance_type_sizes: dict[str, tuple[int, int]],
    identifier_to_ir: dict[str, str],
    network_lookup: dict[str, str],
    subnet_lookup: dict[str, str],
    policy_lookup: dict[str, IntentObject],
    ctx: NormalizeContext,
) -> DatabaseInstance:
    identifier = inst["DBInstanceIdentifier"]
    status = inst["DBInstanceStatus"]
    if status != "available":
        ctx.info(
            "DB-STATUS-TRANSIENT",
            f"{identifier} is in status {status!r}, not 'available'.",
            subject=ir_id,
        )

    role, replica_of = _role(inst, identifier_to_ir)

    return DatabaseInstance(
        id=ir_id,
        name=_name_tag(inst) or identifier,
        labels=clean_labels(_tags(inst)),
        source=_source(ctx, "db_instance", identifier),
        engine=inst["Engine"],
        engine_version=inst["EngineVersion"],
        role=role,
        replica_of=replica_of,
        compute=_compute(inst, instance_type_sizes, ir_id, ctx),
        storage=_storage(inst),
        network=_network(
            inst, ir_id, subnet_groups, network_lookup, subnet_lookup, policy_lookup, ctx
        ),
        high_availability=bool(inst.get("MultiAZ")),
        multi_az_cluster=_is_multiaz_cluster(inst, clusters),
        zone=inst.get("AvailabilityZone"),
        backup=DbBackup(
            retention_days=inst.get("BackupRetentionPeriod", 0),
            window=inst.get("PreferredBackupWindow"),
            copy_tags_to_snapshot=bool(inst.get("CopyTagsToSnapshot")),
        ),
        maintenance=DbMaintenance(
            window=inst.get("PreferredMaintenanceWindow"),
            auto_minor_version_upgrade=inst.get("AutoMinorVersionUpgrade", True),
        ),
        deletion_protection=bool(inst.get("DeletionProtection")),
        master_username=inst.get("MasterUsername"),
        initial_database=inst.get("DBName"),
        parameters=_parameters(inst, db_parameters),
        iam_auth=bool(inst.get("IAMDatabaseAuthenticationEnabled")),
        log_exports=sorted(inst.get("EnabledCloudwatchLogsExports") or []),
        ca_certificate=inst.get("CACertificateIdentifier"),
        status=None if status == "available" else status,
    )
