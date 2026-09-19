"""Compute intent objects (spec 4 §4.2). Faithful capture only; no translatability judgement."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Literal

from pydantic import AfterValidator, Field, model_validator

from easymigrate.ir.base import IntentObject, StrictModel
from easymigrate.ir.network import PolicyRef, SubnetRef

Architecture = Literal["x86_64", "arm64", "i386"]
InstanceState = Literal["running", "stopped"]
Tenancy = Literal["default", "dedicated", "host"]
Platform = Literal["linux", "windows"]
DiskType = Literal["gp2", "gp3", "io1", "io2", "st1", "sc1", "standard"]
HealthCheckType = Literal["EC2", "ELB"]

_IOPS_TYPES = {"gp3", "io1", "io2"}
_THROUGHPUT_TYPES = {"gp3"}
_EXTERNAL_IP_LITERALS = {"none", "ephemeral"}


def _ref(kind: str) -> Callable[[str], str]:
    def check(value: str) -> str:
        prefix = f"{kind}:"
        if not value.startswith(prefix) or value == prefix:
            raise ValueError(f"expected a {kind} reference, got {value!r}")
        return value

    return check


def _external_ip(value: str) -> str:
    if value in _EXTERNAL_IP_LITERALS:
        return value
    if value.startswith("external_address:") and value != "external_address:":
        return value
    raise ValueError(
        f"external_ip {value!r} must be 'none', 'ephemeral' or an external_address reference"
    )


DiskRef = Annotated[str, AfterValidator(_ref("disk"))]
AddressRef = Annotated[str, AfterValidator(_ref("external_address"))]
TemplateRef = Annotated[str, AfterValidator(_ref("instance_template"))]
ExternalIp = Annotated[str, AfterValidator(_external_ip)]


class ImdsOptions(StrictModel):
    http_tokens: Literal["optional", "required"]
    hop_limit: int = Field(ge=1, le=64)


class ScalingPolicy(StrictModel):
    name: str
    policy_type: str
    metric: str | None = None
    target_value: float | None = None


class Disk(IntentObject):
    kind: Literal["disk"] = "disk"
    size_gib: int | None = None
    type: DiskType | None = None
    iops: int | None = None
    throughput_mibps: int | None = None
    encrypted: bool = False
    kms_key: str | None = None
    zone: str
    boot: bool = False
    instance_store: bool = False

    @model_validator(mode="after")
    def _storage_shape_is_consistent(self) -> Disk:
        if self.instance_store:
            if self.type is not None:
                raise ValueError("instance_store disks have no EBS type")
            if self.iops is not None or self.throughput_mibps is not None:
                raise ValueError("instance_store disks have no provisioned iops or throughput")
            return self
        if self.type is None:
            raise ValueError("type is required unless instance_store is set")
        if self.iops is not None and self.type not in _IOPS_TYPES:
            raise ValueError(f"iops is not allowed for disk type {self.type!r}")
        if self.throughput_mibps is not None and self.type not in _THROUGHPUT_TYPES:
            raise ValueError(f"throughput_mibps is not allowed for disk type {self.type!r}")
        return self


class ExternalAddress(IntentObject):
    kind: Literal["external_address"] = "external_address"
    address: str
    attached: bool = False
    scope: Literal["vpc", "standard"] = "vpc"


class _InstanceShape(StrictModel):
    """Fields shared by a running instance and the template that would create one.

    A mixin rather than an `IntentObject` subclass: `IntentObject.__pydantic_init_subclass__`
    requires every subclass to declare a `kind` default, which an abstract shape has no business
    doing.
    """

    instance_type: str
    vcpus: int | None = None
    memory_mib: int | None = None
    architecture: Architecture
    image: str
    platform: Platform
    platform_details: str | None = None
    firewall_policies: list[PolicyRef] = Field(default_factory=list)
    disks: list[DiskRef] = Field(default_factory=list)
    user_data: str | None = None
    key_pair: str | None = None
    key_material: str | None = None
    iam_profile: str | None = None
    spot: bool = False
    imds: ImdsOptions | None = None


class ComputeInstance(_InstanceShape, IntentObject):
    kind: Literal["compute_instance"] = "compute_instance"
    state: InstanceState
    zone: str
    zone_id: str | None = None
    subnet: SubnetRef
    external_ip: ExternalIp = "none"
    private_ip: str | None = None
    tenancy: Tenancy = "default"
    placement_group: str | None = None
    termination_protection: bool = False
    monitoring: bool = False
    eks_cluster: str | None = None

    def references(self) -> list[str]:
        refs: list[str] = [self.subnet, *self.firewall_policies, *self.disks]
        if self.external_ip not in _EXTERNAL_IP_LITERALS:
            refs.append(self.external_ip)
        return refs


class InstanceTemplate(_InstanceShape, IntentObject):
    kind: Literal["instance_template"] = "instance_template"
    version: str
    default_version: str | None = None

    def references(self) -> list[str]:
        return [*self.firewall_policies, *self.disks]


class InstanceGroup(IntentObject):
    kind: Literal["instance_group"] = "instance_group"
    template: TemplateRef
    min_size: int = Field(ge=0)
    max_size: int = Field(ge=0)
    desired_capacity: int = Field(ge=0)
    zones: list[str] = Field(default_factory=list)
    subnets: list[SubnetRef] = Field(default_factory=list)
    health_check_type: HealthCheckType = "EC2"
    health_check_grace: int | None = None
    target_groups: list[str] = Field(default_factory=list)
    scaling_policies: list[ScalingPolicy] = Field(default_factory=list)
    mixed_instances: bool = False

    @model_validator(mode="after")
    def _sizes_are_ordered(self) -> InstanceGroup:
        if not self.min_size <= self.desired_capacity <= self.max_size:
            raise ValueError(
                "desired_capacity must satisfy min_size <= desired_capacity <= max_size, "
                f"got {self.min_size} <= {self.desired_capacity} <= {self.max_size}"
            )
        return self

    def references(self) -> list[str]:
        return [self.template, *self.subnets]
