"""Tiny builders for RDS/EC2-shaped API dicts, used by collector and normalizer unit tests."""

from typing import Any

from easymigrate.core.plugin import Snapshot

OWNER = "123456789012"


def _tags(name: str | None, extra: dict[str, str] | None = None) -> list[dict[str, str]]:
    tags = dict(extra or {})
    if name is not None:
        tags["Name"] = name
    return [{"Key": key, "Value": value} for key, value in tags.items()]


def db_instance(
    identifier: str,
    *,
    engine: str = "postgres",
    engine_version: str = "15.5",
    instance_class: str = "db.t3.micro",
    allocated_storage: int = 20,
    max_allocated_storage: int | None = None,
    storage_type: str = "gp2",
    iops: int | None = None,
    storage_throughput: int | None = None,
    storage_encrypted: bool = False,
    kms_key_id: str | None = None,
    subnet_group_name: str | None = None,
    security_group_ids: tuple[str, ...] = (),
    publicly_accessible: bool = False,
    port: int = 5432,
    has_endpoint: bool = True,
    multi_az: bool = False,
    availability_zone: str = "ap-south-1a",
    backup_retention: int = 0,
    backup_window: str | None = None,
    copy_tags_to_snapshot: bool = False,
    maintenance_window: str | None = None,
    auto_minor_version_upgrade: bool = True,
    deletion_protection: bool = False,
    master_username: str | None = "postgres",
    db_name: str | None = None,
    iam_auth: bool = False,
    log_exports: tuple[str, ...] = (),
    ca_certificate: str | None = None,
    status: str = "available",
    parameter_group_names: tuple[str, ...] = (),
    read_replica_source_id: str | None = None,
    db_cluster_identifier: str | None = None,
    name: str | None = None,
    tags: dict[str, str] | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "DBInstanceIdentifier": identifier,
        "DBInstanceStatus": status,
        "Engine": engine,
        "EngineVersion": engine_version,
        "DBInstanceClass": instance_class,
        "AllocatedStorage": allocated_storage,
        "StorageType": storage_type,
        "StorageEncrypted": storage_encrypted,
        "PubliclyAccessible": publicly_accessible,
        "MultiAZ": multi_az,
        "AvailabilityZone": availability_zone,
        "BackupRetentionPeriod": backup_retention,
        "CopyTagsToSnapshot": copy_tags_to_snapshot,
        "AutoMinorVersionUpgrade": auto_minor_version_upgrade,
        "DeletionProtection": deletion_protection,
        "IAMDatabaseAuthenticationEnabled": iam_auth,
        "EnabledCloudwatchLogsExports": list(log_exports),
        "VpcSecurityGroups": [
            {"VpcSecurityGroupId": gid, "Status": "active"} for gid in security_group_ids
        ],
        "DBParameterGroups": [
            {"DBParameterGroupName": pg, "ParameterApplyStatus": "in-sync"}
            for pg in parameter_group_names
        ],
        # Real DescribeDBInstances reports tags under "TagList", not "Tags" (verified against
        # moto/boto3); task 2's original builder used "Tags", which normalize.py cannot read.
        "TagList": _tags(name, tags),
    }
    if has_endpoint:
        # A `creating` instance has no Endpoint at all yet; real AWS omits the key entirely.
        data["Endpoint"] = {
            "Address": f"{identifier}.example.ap-south-1.rds.amazonaws.com",
            "Port": port,
        }
    if max_allocated_storage is not None:
        data["MaxAllocatedStorage"] = max_allocated_storage
    if iops is not None:
        data["Iops"] = iops
    if storage_throughput is not None:
        data["StorageThroughput"] = storage_throughput
    if kms_key_id is not None:
        data["KmsKeyId"] = kms_key_id
    if subnet_group_name is not None:
        data["DBSubnetGroup"] = {"DBSubnetGroupName": subnet_group_name}
    if backup_window is not None:
        data["PreferredBackupWindow"] = backup_window
    if maintenance_window is not None:
        data["PreferredMaintenanceWindow"] = maintenance_window
    if master_username is not None:
        data["MasterUsername"] = master_username
    if db_name is not None:
        data["DBName"] = db_name
    if ca_certificate is not None:
        data["CACertificateIdentifier"] = ca_certificate
    if read_replica_source_id is not None:
        data["ReadReplicaSourceDBInstanceIdentifier"] = read_replica_source_id
    if db_cluster_identifier is not None:
        data["DBClusterIdentifier"] = db_cluster_identifier
    return data


def db_subnet_group(
    name: str, vpc_id: str, subnet_ids: tuple[str, ...], *, zones: tuple[str, ...] = ()
) -> dict[str, Any]:
    zones = zones or tuple(f"ap-south-1{c}" for c in "abc")
    return {
        "DBSubnetGroupName": name,
        "VpcId": vpc_id,
        "Subnets": [
            {
                "SubnetIdentifier": sid,
                "SubnetAvailabilityZone": {"Name": zones[i % len(zones)]},
            }
            for i, sid in enumerate(subnet_ids)
        ],
    }


def parameter_group(name: str, family: str = "postgres15") -> dict[str, Any]:
    return {"DBParameterGroupName": name, "DBParameterGroupFamily": family}


def parameter(name: str, value: str | None, *, source: str = "user") -> dict[str, Any]:
    data: dict[str, Any] = {"ParameterName": name, "Source": source}
    if value is not None:
        data["ParameterValue"] = value
    return data


def db_cluster(
    identifier: str, *, engine: str = "postgres", members: tuple[str, ...] = ()
) -> dict[str, Any]:
    return {
        "DBClusterIdentifier": identifier,
        "Engine": engine,
        "DBClusterMembers": [{"DBInstanceIdentifier": m} for m in members],
    }


def instance_type(class_name: str, *, vcpus: int, memory_mib: int) -> dict[str, Any]:
    return {
        "InstanceType": class_name,
        "VCpuInfo": {"DefaultVCpus": vcpus},
        "MemoryInfo": {"SizeInMiB": memory_mib},
    }


def snapshot(
    *,
    db_instances: list[dict[str, Any]] = (),
    db_subnet_groups: list[dict[str, Any]] = (),
    db_parameter_groups: list[dict[str, Any]] = (),
    db_parameters: dict[str, list[dict[str, Any]]] = None,
    db_clusters: list[dict[str, Any]] = (),
    instance_types: list[dict[str, Any]] = (),
    extra_files: dict[str, Any] | None = None,
    omit: tuple[str, ...] = (),
) -> Snapshot:
    """Build a database Snapshot, optionally merged with extra (e.g. network.*) files."""
    files: dict[str, Any] = {
        "database.db_instances": {"DBInstances": list(db_instances)},
        "database.db_subnet_groups": {"DBSubnetGroups": list(db_subnet_groups)},
        "database.db_parameter_groups": {"DBParameterGroups": list(db_parameter_groups)},
        "database.db_parameters": dict(db_parameters or {}),
        "database.db_clusters": {"DBClusters": list(db_clusters)},
        "database.instance_types": {"InstanceTypes": list(instance_types)},
    }
    files.update(extra_files or {})
    for name in omit:
        files.pop(name, None)
    return Snapshot(files)
