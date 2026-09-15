"""Security groups → firewall policies and network ACLs from an AWS snapshot (spec §3.2–§3.3).

Faithful capture only: nothing here decides whether GCP can reproduce what it records."""

from __future__ import annotations

from typing import Any

from easymigrate.core.plugin import NormalizeContext, Snapshot
from easymigrate.ir.base import SourceRef
from easymigrate.ir.naming import allocate_ids, clean_labels
from easymigrate.ir.network import (
    AclRule,
    CidrPeer,
    ExternalPolicyPeer,
    FirewallPolicy,
    FirewallRule,
    NetworkAcl,
    PolicyPeer,
    PrefixListPeer,
    SelfPeer,
)
from easymigrate.plugins.network import aws_values as av
from easymigrate.plugins.network.normalize_topology import Topology

_IMPLICIT_DENY = 32767
_EVERYWHERE = frozenset({"0.0.0.0/0", "::/0"})


def build_security(
    snapshot: Snapshot, ctx: NormalizeContext, topology: Topology
) -> tuple[list[FirewallPolicy], list[NetworkAcl]]:
    return _policies(snapshot, ctx, topology), _acls(snapshot, ctx, topology)


def _source(ctx: NormalizeContext, type_: str, native_id: str) -> SourceRef:
    return SourceRef(cloud="aws", type=type_, id=native_id, region=ctx.region)


def _policies(
    snapshot: Snapshot, ctx: NormalizeContext, topology: Topology
) -> list[FirewallPolicy]:
    groups = [
        g
        for g in snapshot.get("network.security_groups") or []
        if g["VpcId"] in topology.network_ids
    ]
    names = {g["GroupId"]: av.name_tag(g) or g["GroupName"] for g in groups}
    policy_ids = allocate_ids(
        "firewall_policy", [(names[g["GroupId"]], g["GroupId"]) for g in groups]
    )
    vpc_of = {g["GroupId"]: g["VpcId"] for g in groups}

    policies = []
    for group in groups:
        rules = [
            rule
            for direction, key in (("ingress", "IpPermissions"), ("egress", "IpPermissionsEgress"))
            for permission in group.get(key) or []
            for rule in _rules(direction, permission, group, policy_ids, vpc_of)
        ]
        rules.sort(key=_rule_sort_key)
        policies.append(
            FirewallPolicy(
                id=policy_ids[group["GroupId"]],
                name=names[group["GroupId"]],
                labels=clean_labels(av.tags(group)),
                source=_source(ctx, "security_group", group["GroupId"]),
                network=topology.network_ids[group["VpcId"]],
                rules=rules,
            )
        )
    return policies


def _rules(
    direction: str,
    permission: dict[str, Any],
    group: dict[str, Any],
    policy_ids: dict[str, str],
    vpc_of: dict[str, str],
) -> list[FirewallRule]:
    protocol = av.protocol(permission["IpProtocol"])
    from_port, to_port = permission.get("FromPort"), permission.get("ToPort")
    ports = av.ports(protocol, from_port, to_port)
    icmp_type, icmp_code = av.icmp(protocol, from_port, to_port)

    peers: list[tuple[Any, str | None]] = []
    for entry in permission.get("IpRanges") or []:
        peers.append((CidrPeer(cidr=av.cidr(entry["CidrIp"])), entry.get("Description")))
    for entry in permission.get("Ipv6Ranges") or []:
        peers.append((CidrPeer(cidr=av.cidr(entry["CidrIpv6"])), entry.get("Description")))
    for entry in permission.get("PrefixListIds") or []:
        peers.append(
            (PrefixListPeer(prefix_list_id=entry["PrefixListId"]), entry.get("Description"))
        )
    for entry in permission.get("UserIdGroupPairs") or []:
        peers.append((_group_peer(entry, group, policy_ids, vpc_of), entry.get("Description")))

    return [
        FirewallRule(
            direction=direction,
            protocol=protocol,
            ports=ports,
            peer=peer,
            description=description,
            icmp_type=icmp_type,
            icmp_code=icmp_code,
        )
        for peer, description in peers
    ]


def _group_peer(
    pair: dict[str, Any],
    group: dict[str, Any],
    policy_ids: dict[str, str],
    vpc_of: dict[str, str],
) -> SelfPeer | PolicyPeer | ExternalPolicyPeer:
    group_id = pair.get("GroupId") or pair.get("GroupName") or "unknown"
    if group_id == group["GroupId"]:
        return SelfPeer()
    same_owner = pair.get("UserId") in (None, group.get("OwnerId"))
    same_vpc = vpc_of.get(group_id) == group["VpcId"] and pair.get("VpcId") in (
        None,
        group["VpcId"],
    )
    if group_id in policy_ids and same_owner and same_vpc:
        return PolicyPeer(policy=policy_ids[group_id])
    return ExternalPolicyPeer(
        group_id=group_id,
        owner_id=pair.get("UserId"),
        vpc_id=pair.get("VpcId") or vpc_of.get(group_id),
    )


def _rule_sort_key(rule: FirewallRule) -> tuple[Any, ...]:
    return (
        rule.direction,
        rule.protocol,
        rule.ports,
        -1 if rule.icmp_type is None else rule.icmp_type,
        -1 if rule.icmp_code is None else rule.icmp_code,
        rule.peer.model_dump_json(),
        rule.description or "",
    )


def _acls(snapshot: Snapshot, ctx: NormalizeContext, topology: Topology) -> list[NetworkAcl]:
    kept = [
        acl
        for acl in snapshot.get("network.network_acls") or []
        if acl["VpcId"] in topology.network_ids and not _is_allow_all(acl.get("Entries") or [])
    ]
    acl_ids = allocate_ids("network_acl", [(av.name_tag(a), a["NetworkAclId"]) for a in kept])
    return [
        NetworkAcl(
            id=acl_ids[acl["NetworkAclId"]],
            name=av.name_tag(acl) or acl["NetworkAclId"],
            labels=clean_labels(av.tags(acl)),
            source=_source(ctx, "network_acl", acl["NetworkAclId"]),
            network=topology.network_ids[acl["VpcId"]],
            subnets=sorted(
                topology.subnet_ids[a["SubnetId"]]
                for a in acl.get("Associations") or []
                if a.get("SubnetId") in topology.subnet_ids
            ),
            rules=sorted(
                (
                    _acl_rule(entry)
                    for entry in acl.get("Entries") or []
                    if entry["RuleNumber"] != _IMPLICIT_DENY
                ),
                key=lambda rule: (rule.direction, rule.number),
            ),
        )
        for acl in kept
    ]


def _is_allow_all(entries: list[dict[str, Any]]) -> bool:
    rules = [e for e in entries if e["RuleNumber"] != _IMPLICIT_DENY]
    if {bool(e.get("Egress")) for e in rules} != {True, False}:
        return False
    return all(
        e.get("RuleAction") == "allow"
        and str(e.get("Protocol")) == "-1"
        and (e.get("CidrBlock") or e.get("Ipv6CidrBlock")) in _EVERYWHERE
        for e in rules
    )


def _acl_rule(entry: dict[str, Any]) -> AclRule:
    protocol = av.protocol(entry["Protocol"])
    port_range = entry.get("PortRange") or {}
    icmp_type_code = entry.get("IcmpTypeCode") or {}
    icmp_type, icmp_code = av.icmp(protocol, icmp_type_code.get("Type"), icmp_type_code.get("Code"))
    return AclRule(
        number=entry["RuleNumber"],
        direction="egress" if entry.get("Egress") else "ingress",
        protocol=protocol,
        ports=av.ports(protocol, port_range.get("From"), port_range.get("To")),
        cidr=av.cidr(entry.get("CidrBlock") or entry["Ipv6CidrBlock"]),
        action=entry["RuleAction"],
        icmp_type=icmp_type,
        icmp_code=icmp_code,
    )
