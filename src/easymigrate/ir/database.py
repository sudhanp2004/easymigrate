"""Database intent objects (spec §4.2). Faithful capture only; no translatability judgement."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import Field, model_validator

from easymigrate.ir.base import IntentObject, StrictModel
from easymigrate.ir.network import NetworkRef, PolicyRef, SubnetRef

Engine = Literal["postgres", "mysql"]
StorageType = Literal["gp2", "gp3", "io1", "io2", "standard"]

_PROVISIONABLE_STORAGE_TYPES = {"gp3", "io1", "io2"}
_REPLICA_OF = re.compile(r"^(database_instance:|external:).+")


class DbCompute(StrictModel):
    class_name: str
    vcpus: int | None = None
    memory_mib: int | None = None


class DbStorage(StrictModel):
    size_gib: int
    max_size_gib: int | None = None
    type: StorageType
    iops: int | None = None
    throughput_mibps: int | None = None
    encrypted: bool = False
    kms_key: str | None = None

    @model_validator(mode="after")
    def _iops_and_throughput_only_on_provisionable_types(self) -> DbStorage:
        if self.type not in _PROVISIONABLE_STORAGE_TYPES:
            if self.iops is not None:
                raise ValueError(f"iops is not allowed for storage type {self.type!r}")
            if self.throughput_mibps is not None:
                raise ValueError(f"throughput_mibps is not allowed for storage type {self.type!r}")
        return self


class DbNetwork(StrictModel):
    network: NetworkRef | None = None
    subnets: list[SubnetRef] = Field(default_factory=list)
    publicly_accessible: bool = False
    port: int = Field(ge=1, le=65535)
    firewall_policies: list[PolicyRef] = Field(default_factory=list)


class DbBackup(StrictModel):
    retention_days: int = 0
    window: str | None = None
    copy_tags_to_snapshot: bool = False


class DbMaintenance(StrictModel):
    window: str | None = None
    auto_minor_version_upgrade: bool = True


class DatabaseInstance(IntentObject):
    kind: Literal["database_instance"] = "database_instance"
    engine: Engine
    engine_version: str
    role: Literal["primary", "read_replica"] = "primary"
    replica_of: str | None = None
    compute: DbCompute
    storage: DbStorage
    network: DbNetwork
    high_availability: bool = False
    multi_az_cluster: bool = False
    zone: str | None = None
    backup: DbBackup = DbBackup()
    maintenance: DbMaintenance = DbMaintenance()
    deletion_protection: bool = False
    master_username: str | None = None
    initial_database: str | None = None
    parameters: dict[str, str] = Field(default_factory=dict)
    iam_auth: bool = False
    log_exports: list[str] = Field(default_factory=list)
    ca_certificate: str | None = None
    status: str | None = None

    @model_validator(mode="after")
    def _role_and_replica_of_agree(self) -> DatabaseInstance:
        if self.role == "read_replica" and self.replica_of is None:
            raise ValueError("role='read_replica' requires replica_of")
        if self.role == "primary" and self.replica_of is not None:
            raise ValueError("role='primary' forbids replica_of")
        if self.replica_of is not None and not _REPLICA_OF.match(self.replica_of):
            raise ValueError(
                f"replica_of {self.replica_of!r} must start with "
                "'database_instance:' or 'external:'"
            )
        return self

    def references(self) -> list[str]:
        refs: list[str] = []
        if self.network.network is not None:
            refs.append(self.network.network)
        refs.extend(self.network.subnets)
        refs.extend(self.network.firewall_policies)
        if self.replica_of is not None and self.replica_of.startswith("database_instance:"):
            refs.append(self.replica_of)
        return refs
