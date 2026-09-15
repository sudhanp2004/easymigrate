"""AWS→GCP networking difference rules (spec §4.4, plan 3 rulings 1-3)."""

from __future__ import annotations

import ipaddress
from typing import Any

from easymigrate.differences.model import Difference, DifferenceRule, Strategy
from easymigrate.ir.base import IntentObject, IRDocument
from easymigrate.ir.network import (
    CustomRoute,
    ExternalPolicyPeer,
    FirewallPolicy,
    FirewallRule,
    Nat,
    Network,
    NetworkAcl,
    PolicyPeer,
    PrefixListPeer,
    Subnet,
)

_EXPOSURE_ORDER = ("isolated", "private_nat", "public")


def _smallest_ipv4_supernet(cidrs: list[str]) -> str:
    """The smallest IPv4 CIDR containing every one of `cidrs`."""
    nets = [ipaddress.ip_network(c) for c in cidrs]
    start = min(int(n.network_address) for n in nets)
    end = max(int(n.broadcast_address) for n in nets)
    for prefix in range(32, -1, -1):
        mask = (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF
        network_addr = start & mask
        broadcast_addr = network_addr | (~mask & 0xFFFFFFFF)
        if network_addr <= start and broadcast_addr >= end:
            return f"{ipaddress.IPv4Address(network_addr)}/{prefix}"
    return "0.0.0.0/0"


def _set_hint(obj: IntentObject, key: str, value: Any) -> IntentObject:
    return obj.model_copy(update={"target_hints": {**obj.target_hints, key: value}})


def _add_omitted(obj: IntentObject, n: int) -> IntentObject:
    return _set_hint(obj, "omitted_rules", obj.target_hints.get("omitted_rules", 0) + n)


def _replace_object(doc: IRDocument, updated: IntentObject) -> IRDocument:
    objects = [updated if obj.id == updated.id else obj for obj in doc.objects]
    return doc.model_copy(update={"objects": objects})


class NetZoneRegional(DifferenceRule):
    id = "NET-ZONE-REGIONAL"
    family = "network"
    title = "AWS subnets are single-zone; GCP subnets span the region"
    impact = (
        "AWS subnets each live in one availability zone. A GCP subnet spans an entire "
        "region, so zone isolation at the subnet level cannot be reproduced exactly."
    )
    strategies = (
        Strategy(
            id="keep_1to1",
            title="One GCP subnet per AWS subnet",
            effect="same CIDRs; zone isolation is lost at the subnet level",
            risk="low",
        ),
        Strategy(
            id="merge_per_network",
            title="Merge into one regional subnet per network",
            effect=(
                "one regional subnet covering the smallest CIDR that contains them; IPs may change"
            ),
            risk="high",
        ),
    )
    default = "keep_1to1"

    def detect(self, ir: IRDocument) -> list[Difference]:
        subnets_by_network: dict[str, list[Subnet]] = {}
        for subnet in ir.of_kind(Subnet):
            subnets_by_network.setdefault(subnet.network, []).append(subnet)
        diffs = []
        for network in sorted(ir.of_kind(Network), key=lambda n: n.id):
            subnets = sorted(subnets_by_network.get(network.id, []), key=lambda s: s.id)
            if not subnets:
                continue
            diffs.append(
                self.difference(
                    [network.id, *(s.id for s in subnets)],
                    network=network.id,
                    zones=sorted({s.zone for s in subnets}),
                    subnet_count=len(subnets),
                )
            )
        return diffs

    def apply(self, ir: IRDocument, diff: Difference, strategy_id: str) -> IRDocument:
        doc = ir.model_copy(deep=True)
        network_id = diff.subjects[0]
        if network_id not in {o.id for o in doc.objects}:
            return doc
        if strategy_id == "keep_1to1":
            return doc
        if strategy_id != "merge_per_network":
            raise ValueError(f"unknown strategy {strategy_id!r}")

        old_ids = set(diff.subjects[1:])
        subnets = [doc.get(sid) for sid in diff.subjects[1:]]
        cidr = _smallest_ipv4_supernet([s.cidr for s in subnets])
        internet = max((s.internet for s in subnets), key=_EXPOSURE_ORDER.index)
        map_public_ip = any(s.map_public_ip for s in subnets)
        network_part = network_id.removeprefix("network:")
        new_id = f"subnet:{network_part}-regional"
        network = doc.get(network_id)
        merged = Subnet(
            id=new_id,
            name=f"{network_part}-regional",
            source=network.source.model_copy(
                update={"type": "merged-subnet", "id": ",".join(sorted(old_ids))}
            ),
            network=network_id,
            cidr=cidr,
            zone="regional",
            internet=internet,
            map_public_ip=map_public_ip,
        )

        def rewrite(ids: list[str]) -> list[str]:
            result: list[str] = []
            for ref in ids:
                ref = new_id if ref in old_ids else ref
                if ref not in result:
                    result.append(ref)
            return result

        objects = []
        for obj in doc.objects:
            if obj.id in old_ids:
                continue
            if isinstance(obj, Nat):
                obj = obj.model_copy(update={"serves_subnets": rewrite(obj.serves_subnets)})
            elif isinstance(obj, NetworkAcl):
                obj = obj.model_copy(update={"subnets": rewrite(obj.subnets)})
            elif isinstance(obj, CustomRoute):
                obj = obj.model_copy(update={"subnets": rewrite(obj.subnets)})
            objects.append(obj)
        objects.append(merged)
        return doc.model_copy(update={"objects": objects})


class NetIsolationVmLevel(DifferenceRule):
    id = "NET-ISOLATION-VM-LEVEL"
    family = "network"
    title = "GCP routes are network-wide; isolation must be enforced per VM"
    impact = (
        "An isolated AWS subnet has no route to the internet. On GCP, routes are "
        "network-wide, so isolation for this subnet's resources must come from having "
        "no external IP rather than from subnet routing."
    )
    strategies = (
        Strategy(
            id="no_external_ip",
            title="Enforce isolation with no external IP",
            effect="resources in this subnet get no external IP; later plugins enforce this",
            risk="medium",
        ),
        Strategy(
            id="manual", title="Handle manually", effect="no change made", risk="low", manual=True
        ),
    )
    default = "no_external_ip"

    def detect(self, ir: IRDocument) -> list[Difference]:
        subnets = ir.of_kind(Subnet)
        public_networks = {s.network for s in subnets if s.internet == "public"}
        diffs = []
        for subnet in sorted(subnets, key=lambda s: s.id):
            if subnet.internet == "isolated" and subnet.network in public_networks:
                diffs.append(self.difference([subnet.id], network=subnet.network))
        return diffs

    def apply(self, ir: IRDocument, diff: Difference, strategy_id: str) -> IRDocument:
        doc = ir.model_copy(deep=True)
        subnet_id = diff.subjects[0]
        if subnet_id not in {o.id for o in doc.objects}:
            return doc
        if strategy_id == "manual":
            return doc
        if strategy_id != "no_external_ip":
            raise ValueError(f"unknown strategy {strategy_id!r}")
        return _replace_object(doc, _set_hint(doc.get(subnet_id), "no_external_ip", True))


class NetEgressTag(DifferenceRule):
    id = "NET-EGRESS-TAG"
    family = "network"
    title = "GCP egress rules cannot target another firewall policy's tag"
    impact = (
        "An AWS egress rule can target another security group directly. GCP egress rules "
        "can only target CIDR ranges, so the peer policy's tag must be translated to ranges."
    )
    strategies = (
        Strategy(
            id="peer_attached_ranges",
            title="Target the peer's attached ranges",
            effect=(
                "destination becomes the peer's network CIDR blocks (applies_to is not yet known)"
            ),
            risk="medium",
        ),
        Strategy(
            id="manual",
            title="Handle manually",
            effect="the egress rules are omitted",
            risk="low",
            manual=True,
        ),
    )
    default = "peer_attached_ranges"

    @staticmethod
    def _matching_rules(policy: FirewallPolicy) -> list[FirewallRule]:
        return [
            rule
            for rule in policy.rules
            if rule.direction == "egress" and isinstance(rule.peer, PolicyPeer)
        ]

    def detect(self, ir: IRDocument) -> list[Difference]:
        diffs = []
        for policy in sorted(ir.of_kind(FirewallPolicy), key=lambda p: p.id):
            matching = self._matching_rules(policy)
            if not matching:
                continue
            peers = sorted({rule.peer.policy for rule in matching})
            diffs.append(
                self.difference([policy.id, *peers], rule_count=len(matching), peers=peers)
            )
        return diffs

    def apply(self, ir: IRDocument, diff: Difference, strategy_id: str) -> IRDocument:
        doc = ir.model_copy(deep=True)
        policy_id = diff.subjects[0]
        if policy_id not in {o.id for o in doc.objects}:
            return doc
        policy = doc.get(policy_id)
        matching = self._matching_rules(policy)
        if strategy_id == "peer_attached_ranges":
            ranges = dict(policy.target_hints.get("egress_ranges", {}))
            for peer_id in diff.subjects[1:]:
                peer_policy = doc.get(peer_id)
                peer_network = doc.get(peer_policy.network)
                ranges[peer_id] = list(peer_network.cidr_blocks)
            return _replace_object(doc, _set_hint(policy, "egress_ranges", ranges))
        if strategy_id == "manual":
            remaining = [rule for rule in policy.rules if rule not in matching]
            updated = policy.model_copy(update={"rules": remaining})
            updated = _add_omitted(updated, len(matching))
            return _replace_object(doc, updated)
        raise ValueError(f"unknown strategy {strategy_id!r}")


class NetSgXvpcRef(DifferenceRule):
    id = "NET-SG-XVPC-REF"
    family = "network"
    title = "GCP has no equivalent of a cross-VPC security group reference"
    impact = (
        "An AWS rule can reference a security group in another VPC or account. GCP "
        "firewall rules cannot reference a policy outside this migration."
    )
    strategies = (
        Strategy(
            id="manual",
            title="Handle manually",
            effect="the rule is omitted; recreate it after migrating the peer network",
            risk="low",
            manual=True,
        ),
    )
    default = "manual"

    @staticmethod
    def _matching_rules(policy: FirewallPolicy) -> list[FirewallRule]:
        return [rule for rule in policy.rules if isinstance(rule.peer, ExternalPolicyPeer)]

    def detect(self, ir: IRDocument) -> list[Difference]:
        diffs = []
        for policy in sorted(ir.of_kind(FirewallPolicy), key=lambda p: p.id):
            matching = self._matching_rules(policy)
            if not matching:
                continue
            group_ids = sorted({rule.peer.group_id for rule in matching})
            diffs.append(self.difference([policy.id], group_ids=group_ids))
        return diffs

    def apply(self, ir: IRDocument, diff: Difference, strategy_id: str) -> IRDocument:
        doc = ir.model_copy(deep=True)
        policy_id = diff.subjects[0]
        if policy_id not in {o.id for o in doc.objects}:
            return doc
        if strategy_id != "manual":
            raise ValueError(f"unknown strategy {strategy_id!r}")
        policy = doc.get(policy_id)
        matching = self._matching_rules(policy)
        remaining = [rule for rule in policy.rules if rule not in matching]
        updated = policy.model_copy(update={"rules": remaining})
        updated = _add_omitted(updated, len(matching))
        return _replace_object(doc, updated)


class NetPrefixList(DifferenceRule):
    id = "NET-PREFIX-LIST"
    family = "network"
    title = "GCP firewalls have no equivalent of an AWS managed prefix list"
    impact = (
        "A rule referencing an AWS managed prefix list has no GCP source/destination equivalent."
    )
    strategies = (
        Strategy(
            id="manual",
            title="Handle manually",
            effect="the rule is omitted",
            risk="low",
            manual=True,
        ),
    )
    default = "manual"

    @staticmethod
    def _matching_rules(policy: FirewallPolicy) -> list[FirewallRule]:
        return [rule for rule in policy.rules if isinstance(rule.peer, PrefixListPeer)]

    def detect(self, ir: IRDocument) -> list[Difference]:
        diffs = []
        for policy in sorted(ir.of_kind(FirewallPolicy), key=lambda p: p.id):
            matching = self._matching_rules(policy)
            if not matching:
                continue
            prefix_list_ids = sorted({rule.peer.prefix_list_id for rule in matching})
            diffs.append(self.difference([policy.id], prefix_list_ids=prefix_list_ids))
        return diffs

    def apply(self, ir: IRDocument, diff: Difference, strategy_id: str) -> IRDocument:
        doc = ir.model_copy(deep=True)
        policy_id = diff.subjects[0]
        if policy_id not in {o.id for o in doc.objects}:
            return doc
        if strategy_id != "manual":
            raise ValueError(f"unknown strategy {strategy_id!r}")
        policy = doc.get(policy_id)
        matching = self._matching_rules(policy)
        remaining = [rule for rule in policy.rules if rule not in matching]
        updated = policy.model_copy(update={"rules": remaining})
        updated = _add_omitted(updated, len(matching))
        return _replace_object(doc, updated)


class NetIcmpType(DifferenceRule):
    id = "NET-ICMP-TYPE"
    family = "network"
    title = "GCP firewall rules cannot filter by ICMP type or code"
    impact = (
        "An AWS rule scoped to a specific ICMP type/code becomes broader than the source on GCP."
    )
    strategies = (
        Strategy(
            id="allow_all_icmp",
            title="Allow all ICMP",
            effect="icmp_type/icmp_code are cleared; identical resulting rules are de-duplicated",
            risk="medium",
            risk_note="broader than the source rule",
        ),
        Strategy(
            id="manual",
            title="Handle manually",
            effect="the rule is omitted",
            risk="low",
            manual=True,
        ),
    )
    default = "allow_all_icmp"

    @staticmethod
    def _matching_rules(policy: FirewallPolicy) -> list[FirewallRule]:
        return [rule for rule in policy.rules if rule.icmp_type is not None]

    def detect(self, ir: IRDocument) -> list[Difference]:
        diffs = []
        for policy in sorted(ir.of_kind(FirewallPolicy), key=lambda p: p.id):
            if self._matching_rules(policy):
                diffs.append(self.difference([policy.id]))
        return diffs

    def apply(self, ir: IRDocument, diff: Difference, strategy_id: str) -> IRDocument:
        doc = ir.model_copy(deep=True)
        policy_id = diff.subjects[0]
        if policy_id not in {o.id for o in doc.objects}:
            return doc
        policy = doc.get(policy_id)
        matching = self._matching_rules(policy)
        if strategy_id == "allow_all_icmp":
            cleared = [
                rule.model_copy(update={"icmp_type": None, "icmp_code": None})
                if rule in matching
                else rule
                for rule in policy.rules
            ]
            deduped: list[FirewallRule] = []
            for rule in cleared:
                if rule not in deduped:
                    deduped.append(rule)
            updated = policy.model_copy(update={"rules": deduped})
            return _replace_object(doc, updated)
        if strategy_id == "manual":
            remaining = [rule for rule in policy.rules if rule not in matching]
            updated = policy.model_copy(update={"rules": remaining})
            updated = _add_omitted(updated, len(matching))
            return _replace_object(doc, updated)
        raise ValueError(f"unknown strategy {strategy_id!r}")
