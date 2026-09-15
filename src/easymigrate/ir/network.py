"""Networking intent objects (spec §3.2). Faithful capture only; no translatability judgement."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable
from typing import Annotated, Literal

from pydantic import AfterValidator, Field, model_validator

from easymigrate.ir.base import IntentObject, StrictModel


def _cidr(version: int | None) -> Callable[[str], str]:
    def check(value: str) -> str:
        if "/" not in value:
            raise ValueError(f"{value!r} is not a valid CIDR: missing prefix length")
        try:
            net = ipaddress.ip_network(value, strict=True)
        except ValueError as exc:
            raise ValueError(f"{value!r} is not a valid CIDR: {exc}") from None
        if version is not None and net.version != version:
            raise ValueError(f"{value!r} is not an IPv{version} CIDR")
        return value

    return check


def _ref(kind: str) -> Callable[[str], str]:
    def check(value: str) -> str:
        prefix = f"{kind}:"
        if not value.startswith(prefix) or value == prefix:
            raise ValueError(f"expected a {kind} reference, got {value!r}")
        return value

    return check


Cidr = Annotated[str, AfterValidator(_cidr(None))]
Cidr4 = Annotated[str, AfterValidator(_cidr(4))]
Cidr6 = Annotated[str, AfterValidator(_cidr(6))]
NetworkRef = Annotated[str, AfterValidator(_ref("network"))]
SubnetRef = Annotated[str, AfterValidator(_ref("subnet"))]
PolicyRef = Annotated[str, AfterValidator(_ref("firewall_policy"))]
Direction = Literal["ingress", "egress"]

_PROTOCOL = re.compile(r"^(tcp|udp|icmp|icmpv6|all|\d{1,3})$")
_PORT = re.compile(r"^(\d{1,5})(?:-(\d{1,5}))?$")
_PORTED_PROTOCOLS = {"tcp", "udp"}


def _check_protocol_and_ports(protocol: str, ports: list[str]) -> None:
    if not _PROTOCOL.match(protocol) or (protocol.isdigit() and int(protocol) > 255):
        raise ValueError(f"unsupported protocol {protocol!r}")
    if ports and protocol not in _PORTED_PROTOCOLS:
        raise ValueError(f"ports are only allowed for tcp/udp, not {protocol!r}")
    for port in ports:
        match = _PORT.match(port)
        if not match:
            raise ValueError(f"invalid port {port!r}")
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if not 0 <= start <= end <= 65535:
            raise ValueError(f"invalid port range {port!r}")


class CidrPeer(StrictModel):
    type: Literal["cidr"] = "cidr"
    cidr: Cidr


class PolicyPeer(StrictModel):
    """Another firewall policy in the same migration (AWS: security group in the same VPC)."""

    type: Literal["policy"] = "policy"
    policy: PolicyRef


class ExternalPolicyPeer(StrictModel):
    """A security group outside this migration: another VPC or another account."""

    type: Literal["external_policy"] = "external_policy"
    group_id: str
    owner_id: str | None = None
    vpc_id: str | None = None


class SelfPeer(StrictModel):
    type: Literal["self"] = "self"


class PrefixListPeer(StrictModel):
    """An AWS managed prefix list reference."""

    type: Literal["prefix_list"] = "prefix_list"
    prefix_list_id: str


Peer = Annotated[
    CidrPeer | PolicyPeer | ExternalPolicyPeer | SelfPeer | PrefixListPeer,
    Field(discriminator="type"),
]


def _check_icmp(protocol: str, icmp_type: int | None, icmp_code: int | None) -> None:
    if icmp_type is None and icmp_code is None:
        return
    if protocol not in ("icmp", "icmpv6"):
        raise ValueError("icmp_type/icmp_code are only allowed for icmp/icmpv6")
    if icmp_code is not None and icmp_type is None:
        raise ValueError("icmp_code requires icmp_type")


class FirewallRule(StrictModel):
    direction: Direction
    protocol: str
    ports: list[str] = Field(default_factory=list)
    peer: Peer
    description: str | None = None
    icmp_type: int | None = Field(default=None, ge=0, le=255)
    icmp_code: int | None = Field(default=None, ge=0, le=255)

    @model_validator(mode="after")
    def _valid_protocol_and_ports(self) -> FirewallRule:
        _check_protocol_and_ports(self.protocol, self.ports)
        _check_icmp(self.protocol, self.icmp_type, self.icmp_code)
        return self


class AclRule(StrictModel):
    number: int = Field(ge=1, le=32766)
    direction: Direction
    protocol: str
    ports: list[str] = Field(default_factory=list)
    cidr: Cidr
    action: Literal["allow", "deny"]
    icmp_type: int | None = Field(default=None, ge=0, le=255)
    icmp_code: int | None = Field(default=None, ge=0, le=255)

    @model_validator(mode="after")
    def _valid_protocol_and_ports(self) -> AclRule:
        _check_protocol_and_ports(self.protocol, self.ports)
        _check_icmp(self.protocol, self.icmp_type, self.icmp_code)
        return self


class Network(IntentObject):
    kind: Literal["network"] = "network"
    cidr_blocks: list[Cidr4] = Field(min_length=1)
    ipv6_cidr_blocks: list[Cidr6] = Field(default_factory=list)
    dns_hostnames: bool
    dns_resolution: bool


class Subnet(IntentObject):
    kind: Literal["subnet"] = "subnet"
    network: NetworkRef
    cidr: Cidr4
    ipv6_cidr: Cidr6 | None = None
    zone: str
    internet: Literal["public", "private_nat", "isolated"]
    map_public_ip: bool

    def references(self) -> list[str]:
        return [self.network]


class Nat(IntentObject):
    kind: Literal["nat"] = "nat"
    network: NetworkRef
    serves_subnets: list[SubnetRef] = Field(default_factory=list)

    def references(self) -> list[str]:
        return [self.network, *self.serves_subnets]


class FirewallPolicy(IntentObject):
    kind: Literal["firewall_policy"] = "firewall_policy"
    network: NetworkRef
    rules: list[FirewallRule] = Field(default_factory=list)
    applies_to: list[str] = Field(default_factory=list)  # native ids, filled by later plugins

    def references(self) -> list[str]:
        peers = [rule.peer.policy for rule in self.rules if isinstance(rule.peer, PolicyPeer)]
        return [self.network, *peers]


class NetworkAcl(IntentObject):
    kind: Literal["network_acl"] = "network_acl"
    network: NetworkRef
    subnets: list[SubnetRef] = Field(default_factory=list)
    rules: list[AclRule] = Field(default_factory=list)

    def references(self) -> list[str]:
        return [self.network, *self.subnets]


class CustomRoute(IntentObject):
    kind: Literal["custom_route"] = "custom_route"
    network: NetworkRef
    destination: str  # CIDR or AWS prefix-list id
    target_type: Literal["peering", "vpn", "tgw", "eni", "other"]
    target_id: str
    subnets: list[SubnetRef] = Field(default_factory=list)

    def references(self) -> list[str]:
        return [self.network, *self.subnets]
