"""Kubernetes intent objects (spec 5 §4.2). Faithful capture only; no translatability judgement."""

from __future__ import annotations

import ipaddress
from typing import Literal

from pydantic import Field, field_validator, model_validator

from easymigrate.ir.base import IntentObject, StrictModel
from easymigrate.ir.network import PolicyRef, SubnetRef

TaintEffect = Literal["NO_SCHEDULE", "NO_EXECUTE", "PREFER_NO_SCHEDULE"]


def _canonical_cidr(value: str) -> str:
    return str(ipaddress.ip_network(value, strict=False))


class Taint(StrictModel):
    key: str
    value: str | None = None
    effect: TaintEffect


class Addon(StrictModel):
    name: str
    version: str | None = None


class PodIdentity(StrictModel):
    namespace: str
    service_account: str
    role_arn: str | None = None


class FargateSelector(StrictModel):
    namespace: str
    labels: dict[str, str] = Field(default_factory=dict)


class FargateProfile(StrictModel):
    name: str
    selectors: list[FargateSelector] = Field(default_factory=list)


class KubernetesCluster(IntentObject):
    kind: Literal["kubernetes_cluster"] = "kubernetes_cluster"
    version: str
    subnets: list[SubnetRef] = Field(default_factory=list)
    firewall_policies: list[PolicyRef] = Field(default_factory=list)
    endpoint_public: bool = True
    endpoint_private: bool = False
    public_access_cidrs: list[str] = Field(default_factory=list)
    service_cidr: str | None = None
    ip_family: Literal["ipv4", "ipv6"] = "ipv4"
    log_types: list[str] = Field(default_factory=list)
    secrets_kms_key: str | None = None
    oidc_issuer: str | None = None
    addons: list[Addon] = Field(default_factory=list)
    access_entries: list[str] = Field(default_factory=list)
    pod_identities: list[PodIdentity] = Field(default_factory=list)
    fargate_profiles: list[FargateProfile] = Field(default_factory=list)
    authentication_mode: str | None = None

    @field_validator("public_access_cidrs")
    @classmethod
    def _cidrs(cls, value: list[str]) -> list[str]:
        return [_canonical_cidr(cidr) for cidr in value]

    @field_validator("service_cidr")
    @classmethod
    def _service_cidr(cls, value: str | None) -> str | None:
        return _canonical_cidr(value) if value else None

    def references(self) -> list[str]:
        return [*self.subnets, *self.firewall_policies]


class NodePool(IntentObject):
    kind: Literal["node_pool"] = "node_pool"
    cluster: str
    origin: Literal["managed", "self_managed"]
    instance_types: list[str] = Field(default_factory=list)
    vcpus: int | None = None
    memory_mib: int | None = None
    capacity: Literal["on_demand", "spot"] = "on_demand"
    min_size: int = Field(ge=0)
    max_size: int = Field(ge=0)
    desired_size: int = Field(ge=0)
    subnets: list[SubnetRef] = Field(default_factory=list)
    disk_size_gib: int | None = None
    ami_type: str
    node_labels: dict[str, str] = Field(default_factory=dict)
    taints: list[Taint] = Field(default_factory=list)
    firewall_policies: list[PolicyRef] = Field(default_factory=list)
    ssh_key: str | None = None
    max_unavailable: int | None = None
    max_unavailable_percent: int | None = None
    version: str | None = None

    @field_validator("cluster")
    @classmethod
    def _cluster_ref(cls, value: str) -> str:
        if not value.startswith("kubernetes_cluster:") or value == "kubernetes_cluster:":
            raise ValueError(f"expected a kubernetes_cluster reference, got {value!r}")
        return value

    @model_validator(mode="after")
    def _sizes_are_ordered(self) -> NodePool:
        if not self.min_size <= self.desired_size <= self.max_size:
            raise ValueError(
                "desired_size must satisfy min_size <= desired_size <= max_size, "
                f"got {self.min_size} <= {self.desired_size} <= {self.max_size}"
            )
        return self

    def references(self) -> list[str]:
        return [self.cluster, *self.subnets, *self.firewall_policies]
