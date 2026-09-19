"""AWS→GCP database difference rules, RDS -> Cloud SQL (spec §5, plan 2 Part C).

This module carries all 20 rules of the `DB-*` catalogue: engine version, tier,
connectivity (private IP / public access), the security-group firewall rule that
Cloud SQL does not need, multi-AZ, storage autoresize/type/minimum, parameter flags,
backup window, point-in-time recovery, maintenance window, port, CMEK, IAM auth, log
exports, read replicas, users and instance-name reuse. Order here is load-bearing
(spec §5): connectivity rules run before `DB-SG-FIREWALL`, and `DB-ENGINE-VERSION`
runs before `DB-TIER`.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any

from easymigrate.differences.membership import policy_members
from easymigrate.differences.model import Difference, DifferenceRule, Risk, Strategy
from easymigrate.differences.rules.aws_gcp.cloudsql_facts import (
    DATABASE_VERSION,
    DAY_NUMBER,
    DEFAULT_PORT,
    FORMULA,
    MAX_RETAINED_BACKUPS,
    MAX_TRANSACTION_LOG_RETENTION_DAYS,
    MEMORY_PER_VCPU_MIB,
    MEMORY_STEP_MIB,
    MIN_CUSTOM_MEMORY_MIB,
    MIN_DISK_GIB,
    OLDEST_SUPPORTED,
    SUPPORTED_FLAGS,
    SUPPORTED_VERSIONS,
)
from easymigrate.ir.base import IntentObject, IRDocument
from easymigrate.ir.database import DatabaseInstance
from easymigrate.ir.network import CidrPeer, FirewallPolicy, Network

# Members that mean a policy is not RDS-only: dropping it would strip a migrated workload's
# firewall rules (spec 4 §2.1.1).
_NON_DATABASE_MEMBERS = frozenset({"compute_instance", "instance_template"})

# ---- shared helpers (mirrors network.py's idiom) --------------------------


def _is_world_cidr(cidr: str) -> bool:
    """True for a CIDR that covers the whole IPv4 or IPv6 address space. An AWS security
    group's `Ipv6Ranges` normalize into ordinary `CidrPeer`s (spec's network normalizer),
    so `::/0` reaches `authorized_networks` exactly like `0.0.0.0/0` and must be treated
    as equally open to the world (I4) — an exact string match on `"0.0.0.0/0"` misses it."""
    try:
        network = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return False
    return network.prefixlen == 0


def _exclude(obj: IntentObject, reason: str) -> IntentObject:
    return obj.model_copy(update={"excluded": reason})


def _set_hint(obj: IntentObject, key: str, value: Any) -> IntentObject:
    return obj.model_copy(update={"target_hints": {**obj.target_hints, key: value}})


def _replace_object(doc: IRDocument, updated: IntentObject) -> IRDocument:
    objects = [updated if obj.id == updated.id else obj for obj in doc.objects]
    return doc.model_copy(update={"objects": objects})


def _network_id_of(ir: IRDocument, instance: DatabaseInstance) -> str:
    if instance.network.network is not None:
        return instance.network.network
    for subnet_ref in instance.network.subnets:
        return ir.get(subnet_ref).network
    raise ValueError(f"{instance.id} has no attached network or subnet")


_PORT_RANGE = re.compile(r"^(\d{1,5})(?:-(\d{1,5}))?$")


def _port_covers(ports: list[str], port: int) -> bool:
    """AWS convention: an empty port list on a rule means every port."""
    if not ports:
        return True
    for spec in ports:
        match = _PORT_RANGE.match(spec)
        if not match:
            continue
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if start <= port <= end:
            return True
    return False


def _derive_authorized_networks(ir: IRDocument, instance: DatabaseInstance) -> list[str]:
    """Every CidrPeer CIDR in an ingress rule of an attached policy covering the DB port."""
    cidrs: set[str] = set()
    for policy_id in instance.network.firewall_policies:
        policy = ir.get(policy_id)
        if policy.excluded is not None:
            continue
        for rule in policy.rules:
            if rule.direction != "ingress" or not isinstance(rule.peer, CidrPeer):
                continue
            if _port_covers(rule.ports, instance.network.port):
                cidrs.add(rule.peer.cidr)
    return sorted(cidrs)


# ---- Private Services Access range allocation (DB-PRIVATE-IP / DB-PUBLIC-ACCESS) ------

_PSA_START = ipaddress.IPv4Address("10.64.0.0")
_PSA_SUPERNET = ipaddress.ip_network("10.0.0.0/8")
_PSA_PREFIX_LENGTH = 24


def _allocate_psa_ranges(
    ir: IRDocument, network_ids: list[str], prefix_length: int = _PSA_PREFIX_LENGTH
) -> dict[str, str]:
    """One /prefix_length per network, scanning 10.0.0.0/8 from 10.64.0.0 upward,
    skipping any range that overlaps a scanned VPC CIDR or an earlier allocation."""
    vpc_networks = [ipaddress.ip_network(c) for net in ir.of_kind(Network) for c in net.cidr_blocks]
    used: list[ipaddress.IPv4Network] = []
    allocated: dict[str, str] = {}
    step = 2 ** (32 - prefix_length)
    for network_id in sorted(set(network_ids)):
        candidate_addr = _PSA_START
        while True:
            if candidate_addr not in _PSA_SUPERNET:
                raise ValueError("exhausted 10.0.0.0/8 while allocating a PSA range")
            candidate = ipaddress.ip_network(f"{candidate_addr}/{prefix_length}")
            if not any(candidate.overlaps(v) for v in vpc_networks) and not any(
                candidate.overlaps(u) for u in used
            ):
                allocated[network_id] = str(candidate.network_address)
                used.append(candidate)
                break
            candidate_addr = ipaddress.IPv4Address(int(candidate_addr) + step)
    return allocated


def _vpc_cidrs_context(ir: IRDocument) -> dict[str, list[str]]:
    return {
        net.id: list(net.cidr_blocks) for net in sorted(ir.of_kind(Network), key=lambda n: n.id)
    }


def _connectivity_network_ids(ir: IRDocument) -> list[str]:
    """Every network a PSA range might be allocated for: DB-PRIVATE-IP always needs one
    for a non-public instance, and DB-PUBLIC-ACCESS's `private_only` strategy needs one
    for a public instance too. Both rules allocate from this single combined list so
    they can never hand two different networks the same range (spec §5)."""
    return [
        _network_id_of(ir, instance)
        for instance in sorted(ir.of_kind(DatabaseInstance), key=lambda i: i.id)
    ]


def _psa_default_params(diff: Difference) -> dict[str, Any]:
    return {"range": diff.context["psa_range"], "prefix_length": diff.context["psa_prefix_length"]}


def _validate_psa_params(diff: Difference, params: dict[str, Any]) -> None:
    range_ = params.get("range")
    prefix_length = params.get("prefix_length")
    try:
        address = ipaddress.IPv4Address(range_)
    except (ValueError, TypeError):
        raise ValueError(f"range {range_!r} is not a valid IPv4 address") from None
    if not isinstance(prefix_length, int) or not (16 <= prefix_length <= 24):
        raise ValueError(f"prefix_length {prefix_length!r} must be between 16 and 24")
    try:
        candidate = ipaddress.ip_network(f"{address}/{prefix_length}", strict=True)
    except ValueError:
        raise ValueError(
            f"range {range_!r} is not the network address for prefix length {prefix_length}"
        ) from None
    for vpc_id, cidrs in diff.context["vpc_cidrs"].items():
        for cidr in cidrs:
            if candidate.overlaps(ipaddress.ip_network(cidr)):
                raise ValueError(f"range {candidate} overlaps VPC {vpc_id}'s CIDR {cidr}")
    this_network = diff.context["network"]
    for network_id, allocated in diff.context["psa_allocations"].items():
        if network_id == this_network:
            continue
        other = ipaddress.ip_network(f"{allocated}/{_PSA_PREFIX_LENGTH}")
        if candidate.overlaps(other):
            raise ValueError(f"range {candidate} overlaps network {network_id}'s allocated range")


# ---- 1. DB-ENGINE-VERSION --------------------------------------------------


def _engine_major(engine: str, engine_version: str) -> str:
    parts = engine_version.split(".")
    if engine == "postgres":
        return parts[0] if int(parts[0]) >= 10 else f"{parts[0]}.{parts[1]}"
    if engine == "mysql":
        return f"{parts[0]}.{parts[1]}"
    raise ValueError(f"unsupported engine {engine!r}")


def _version_key(major: str) -> tuple[int, ...]:
    """Numeric sort key so majors compare by value, not lexicographically
    (`"9.6" < "13"`, `"18" > "17"`)."""
    return tuple(int(part) for part in major.split("."))


class DbEngineVersion(DifferenceRule):
    id = "DB-ENGINE-VERSION"
    family = "database"
    title = "AWS engine versions do not line up with Cloud SQL's supported majors"
    impact = (
        "Cloud SQL supports a specific set of major versions per engine. A source major "
        "outside that set cannot be created as-is; the engine must be upgraded."
    )
    strategies = (
        Strategy(
            id="nearest_major",
            title="Use the nearest Cloud SQL major version",
            effect="database_version set for the source major; the patch level is Google's "
            "to choose",
            risk="low",
        ),
        Strategy(
            id="upgrade_to_oldest_supported",
            title="Upgrade to Cloud SQL's oldest supported major",
            effect="database_version set to the oldest supported major; an engine upgrade "
            "with application-visible behaviour changes",
            risk="high",
        ),
        Strategy(id="manual", title="Handle manually", effect="excluded", risk="low", manual=True),
    )
    default = "nearest_major"

    def default_strategy(self, diff: Difference) -> str:
        return (
            "upgrade_to_oldest_supported" if diff.context["upgrade_required"] else "nearest_major"
        )

    def allowed_strategies(self, diff: Difference) -> tuple[Strategy, ...]:
        ids = (
            {"upgrade_to_oldest_supported", "manual"}
            if diff.context["upgrade_required"]
            else {"nearest_major", "manual"}
        )
        return tuple(s for s in self.strategies if s.id in ids)

    def detect(self, ir: IRDocument) -> list[Difference]:
        diffs = []
        for instance in sorted(ir.of_kind(DatabaseInstance), key=lambda i: i.id):
            major = _engine_major(instance.engine, instance.engine_version)
            supported = SUPPORTED_VERSIONS[instance.engine]
            if major in supported:
                # Exactly supported: no upgrade, no downgrade.
                target = major
                upgrade_required = False
            elif _version_key(major) > _version_key(supported[0]):
                # Newer than anything Cloud SQL supports: the nearest achievable major
                # is the newest supported one, below the source — a downgrade, not an
                # upgrade, so it must not be routed through `upgrade_to_oldest_supported`.
                target = supported[0]
                upgrade_required = False
            else:
                # Older than Cloud SQL's floor: a genuine upgrade is required.
                target = OLDEST_SUPPORTED[instance.engine]
                upgrade_required = True
            diffs.append(
                self.difference(
                    [instance.id],
                    engine=instance.engine,
                    version=instance.engine_version,
                    major=major,
                    target=target,
                    upgrade_required=upgrade_required,
                )
            )
        return diffs

    def apply(
        self,
        ir: IRDocument,
        diff: Difference,
        strategy_id: str,
        params: dict[str, Any] | None = None,
    ) -> IRDocument:
        doc = ir.model_copy(deep=True)
        instance_id = diff.subjects[0]
        if instance_id not in {o.id for o in doc.objects}:
            return doc
        allowed = {s.id for s in self.allowed_strategies(diff)}
        if strategy_id not in allowed:
            raise ValueError(f"unknown strategy {strategy_id!r}")
        instance = doc.get(instance_id)
        if strategy_id == "manual":
            return _replace_object(
                doc, _exclude(instance, "DB-ENGINE-VERSION: engine version requires manual review")
            )
        version = DATABASE_VERSION[(diff.context["engine"], diff.context["target"])]
        return _replace_object(doc, _set_hint(instance, "database_version", version))


# ---- 2. DB-TIER -------------------------------------------------------------

_BURSTABLE_FAMILY_RE = re.compile(r"^db\.t\d*g?\.(micro|small)$")
_SHARED_CORE_BY_SUFFIX = {"micro": "db-f1-micro", "small": "db-g1-small"}


def _shared_core_tier(class_name: str) -> str | None:
    match = _BURSTABLE_FAMILY_RE.match(class_name)
    return _SHARED_CORE_BY_SUFFIX[match.group(1)] if match else None


def _custom_tier_memory_mib(vcpus: int, memory_mib: int) -> int:
    floor = max(vcpus * MEMORY_PER_VCPU_MIB[0], MIN_CUSTOM_MEMORY_MIB)
    ceiling = max(vcpus * MEMORY_PER_VCPU_MIB[1], floor)
    clamped = min(max(memory_mib, floor), ceiling)
    return -(-clamped // MEMORY_STEP_MIB) * MEMORY_STEP_MIB


class DbTier(DifferenceRule):
    id = "DB-TIER"
    family = "database"
    title = "AWS instance classes do not map onto Cloud SQL machine tiers"
    impact = (
        "Cloud SQL bills by custom or shared-core tier, not by AWS instance class name; "
        "the nearest equivalent must be computed from vCPU and memory."
    )
    strategies = (
        Strategy(
            id="nearest_custom_tier",
            title="Use the nearest custom machine tier",
            effect="db-custom-<vcpus>-<memory_mb>, memory rounded into Cloud SQL's per-vCPU "
            "band and to a 256 MB multiple",
            risk="medium",
        ),
        Strategy(
            id="shared_core",
            title="Use a shared-core tier",
            effect="db-f1-micro or db-g1-small, chosen from the source burstable class",
            risk="medium",
        ),
        Strategy(id="manual", title="Handle manually", effect="excluded", risk="low", manual=True),
    )
    default = "nearest_custom_tier"

    def default_strategy(self, diff: Difference) -> str:
        if not diff.context["sizing_known"]:
            return "manual"
        if diff.context["shared_core_tier"] is not None:
            return "shared_core"
        return "nearest_custom_tier"

    def allowed_strategies(self, diff: Difference) -> tuple[Strategy, ...]:
        ids = (
            {"nearest_custom_tier", "shared_core", "manual"}
            if diff.context["sizing_known"]
            else {"manual"}
        )
        return tuple(s for s in self.strategies if s.id in ids)

    def detect(self, ir: IRDocument) -> list[Difference]:
        diffs = []
        for instance in sorted(ir.of_kind(DatabaseInstance), key=lambda i: i.id):
            compute = instance.compute
            diffs.append(
                self.difference(
                    [instance.id],
                    class_name=compute.class_name,
                    vcpus=compute.vcpus,
                    memory_mib=compute.memory_mib,
                    sizing_known=compute.vcpus is not None,
                    shared_core_tier=_shared_core_tier(compute.class_name),
                )
            )
        return diffs

    def apply(
        self,
        ir: IRDocument,
        diff: Difference,
        strategy_id: str,
        params: dict[str, Any] | None = None,
    ) -> IRDocument:
        doc = ir.model_copy(deep=True)
        instance_id = diff.subjects[0]
        if instance_id not in {o.id for o in doc.objects}:
            return doc
        allowed = {s.id for s in self.allowed_strategies(diff)}
        if strategy_id not in allowed:
            raise ValueError(f"unknown strategy {strategy_id!r}")
        instance = doc.get(instance_id)
        if strategy_id == "manual":
            return _replace_object(
                doc, _exclude(instance, "DB-TIER: instance sizing requires manual review")
            )
        if strategy_id == "shared_core":
            tier = diff.context["shared_core_tier"]
            if tier is None:
                raise ValueError(
                    f"{diff.key}: shared_core is not available for class "
                    f"{diff.context['class_name']!r}"
                )
            return _replace_object(doc, _set_hint(instance, "tier", tier))
        vcpus = diff.context["vcpus"]
        memory_mib = diff.context["memory_mib"]
        if memory_mib is None:
            raise ValueError(f"{diff.key}: nearest_custom_tier requires a known memory size")
        mem_mb = _custom_tier_memory_mib(vcpus, memory_mib)
        return _replace_object(doc, _set_hint(instance, "tier", f"db-custom-{vcpus}-{mem_mb}"))


# ---- 3. DB-PRIVATE-IP -------------------------------------------------------


class DbPrivateIp(DifferenceRule):
    id = "DB-PRIVATE-IP"
    family = "database"
    title = "A private RDS instance needs Private Services Access on Cloud SQL"
    impact = (
        "Cloud SQL's private IP is reached through Private Services Access, which needs "
        "an allocated IP range peered to the VPC; there is no direct VPC attachment."
    )
    strategies = (
        Strategy(
            id="psa_auto_range",
            title="Allocate a Private Services Access range",
            effect="a private IP only, via an allocated PSA range peered to the network",
            risk="medium",
        ),
        Strategy(
            id="public_ip_authorized_networks",
            title="Expose a public IP restricted by authorized networks",
            effect="the instance becomes internet-reachable, restricted to the SG's CIDRs",
            risk="high",
        ),
        Strategy(id="manual", title="Handle manually", effect="excluded", risk="low", manual=True),
    )
    default = "psa_auto_range"
    param_docs = {
        "range": "Network address of the allocated Private Services Access range.",
        "prefix_length": "Prefix length of the allocated range (16-24).",
    }

    def detect(self, ir: IRDocument) -> list[Difference]:
        instances = [
            i
            for i in sorted(ir.of_kind(DatabaseInstance), key=lambda i: i.id)
            if not i.network.publicly_accessible
        ]
        if not instances:
            return []
        allocations = _allocate_psa_ranges(ir, _connectivity_network_ids(ir))
        vpc_cidrs = _vpc_cidrs_context(ir)
        diffs = []
        for instance in instances:
            network_id = _network_id_of(ir, instance)
            diffs.append(
                self.difference(
                    [instance.id],
                    network=network_id,
                    psa_range=allocations[network_id],
                    psa_prefix_length=_PSA_PREFIX_LENGTH,
                    vpc_cidrs=vpc_cidrs,
                    psa_allocations=dict(allocations),
                )
            )
        return diffs

    def default_params(self, diff: Difference, strategy_id: str) -> dict[str, Any]:
        if strategy_id == "psa_auto_range":
            return _psa_default_params(diff)
        return {}

    def validate_params(self, diff: Difference, strategy_id: str, params: dict[str, Any]) -> None:
        if strategy_id == "psa_auto_range":
            _validate_psa_params(diff, params)

    def apply(
        self,
        ir: IRDocument,
        diff: Difference,
        strategy_id: str,
        params: dict[str, Any] | None = None,
    ) -> IRDocument:
        doc = ir.model_copy(deep=True)
        instance_id = diff.subjects[0]
        if instance_id not in {o.id for o in doc.objects}:
            return doc
        instance = doc.get(instance_id)
        params = params or {}
        if strategy_id == "psa_auto_range":
            range_ = params.get("range", diff.context["psa_range"])
            prefix_length = params.get("prefix_length", diff.context["psa_prefix_length"])
            updated = _set_hint(instance, "private_network", diff.context["network"])
            updated = _set_hint(updated, "psa", {"range": range_, "prefix_length": prefix_length})
            return _replace_object(doc, updated)
        if strategy_id == "public_ip_authorized_networks":
            networks = _derive_authorized_networks(doc, instance)
            updated = _set_hint(instance, "public_ip", True)
            updated = _set_hint(updated, "authorized_networks", networks)
            return _replace_object(doc, updated)
        if strategy_id == "manual":
            return _replace_object(
                doc,
                _exclude(instance, "DB-PRIVATE-IP: private connectivity requires manual review"),
            )
        raise ValueError(f"unknown strategy {strategy_id!r}")


# ---- 4. DB-PUBLIC-ACCESS ----------------------------------------------------


class DbPublicAccess(DifferenceRule):
    id = "DB-PUBLIC-ACCESS"
    family = "database"
    title = "A publicly accessible RDS instance has no direct Cloud SQL equivalent"
    impact = (
        "Cloud SQL's public IP has no VPC-native concept of a security group; access is "
        "restricted by an authorized-networks CIDR allow list instead."
    )
    strategies = (
        Strategy(
            id="public_ip_authorized_networks",
            title="Restrict the public IP with authorized networks",
            effect="authorized networks derived from the attached policies' ingress CIDRs",
            risk="medium",
        ),
        Strategy(
            id="private_only",
            title="Move to a private IP only",
            effect="matches nothing in the source; every client must move inside the VPC",
            risk="medium",
        ),
        Strategy(
            id="public_ip_allow_cidrs",
            title="Keep the public IP and allow CIDRs you supply",
            effect="authorized networks come from the `cidrs` param, since none could be derived",
            risk="high",
            risk_note=(
                "You are choosing who can reach this database over the internet; nothing in "
                "the source tells EasyMigrate what that should be."
            ),
        ),
        Strategy(id="manual", title="Handle manually", effect="excluded", risk="low", manual=True),
    )
    default = "public_ip_authorized_networks"
    param_docs = {
        "range": "Network address of the allocated Private Services Access range.",
        "prefix_length": "Prefix length of the allocated range (16-24).",
        "cidrs": "CIDRs allowed to reach the public IP, e.g. ['203.0.113.4/32'].",
    }

    @staticmethod
    def _no_derivable_networks(diff: Difference) -> bool:
        """The source is publicly accessible but nothing says who may reach it — its
        security groups reference other security groups rather than CIDRs, which is the
        usual way RDS rules are written. Generating `authorized_networks = []` would
        produce a public Cloud SQL instance that nobody can connect to."""
        return not diff.context["authorized_networks"]

    def default_strategy(self, diff: Difference) -> str:
        if self._no_derivable_networks(diff):
            return "manual"
        return self.default

    def allowed_strategies(self, diff: Difference) -> tuple[Strategy, ...]:
        if self._no_derivable_networks(diff):
            ids = {"public_ip_allow_cidrs", "private_only", "manual"}
        else:
            ids = {"public_ip_authorized_networks", "private_only", "manual"}
        return tuple(s for s in self.strategies if s.id in ids)

    def detect(self, ir: IRDocument) -> list[Difference]:
        instances = [
            i
            for i in sorted(ir.of_kind(DatabaseInstance), key=lambda i: i.id)
            if i.network.publicly_accessible
        ]
        if not instances:
            return []
        allocations = _allocate_psa_ranges(ir, _connectivity_network_ids(ir))
        vpc_cidrs = _vpc_cidrs_context(ir)
        diffs = []
        for instance in instances:
            networks = _derive_authorized_networks(ir, instance)
            network_id = _network_id_of(ir, instance)
            diffs.append(
                self.difference(
                    [instance.id],
                    network=network_id,
                    authorized_networks=networks,
                    open_to_world=any(_is_world_cidr(cidr) for cidr in networks),
                    psa_range=allocations[network_id],
                    psa_prefix_length=_PSA_PREFIX_LENGTH,
                    vpc_cidrs=vpc_cidrs,
                    psa_allocations=dict(allocations),
                )
            )
        return diffs

    def default_params(self, diff: Difference, strategy_id: str) -> dict[str, Any]:
        if strategy_id == "private_only":
            return _psa_default_params(diff)
        if strategy_id == "public_ip_allow_cidrs":
            return {"cidrs": []}
        return {}

    def validate_params(self, diff: Difference, strategy_id: str, params: dict[str, Any]) -> None:
        if strategy_id == "private_only":
            _validate_psa_params(diff, params)
        if strategy_id == "public_ip_allow_cidrs":
            cidrs = params.get("cidrs")
            if not isinstance(cidrs, list) or not cidrs:
                raise ValueError(
                    "cidrs must list at least one CIDR allowed to reach the public IP, "
                    "for example ['203.0.113.4/32']; nothing in the source says who may "
                    "connect, so EasyMigrate will not guess"
                )
            for cidr in cidrs:
                try:
                    ipaddress.ip_network(str(cidr), strict=True)
                except ValueError as exc:
                    raise ValueError(f"cidrs entry {cidr!r} is not a valid CIDR: {exc}") from None

    def risk_for(self, diff: Difference, strategy_id: str) -> Risk:
        if strategy_id == "public_ip_authorized_networks" and diff.context["open_to_world"]:
            return "high"
        return super().risk_for(diff, strategy_id)

    def apply(
        self,
        ir: IRDocument,
        diff: Difference,
        strategy_id: str,
        params: dict[str, Any] | None = None,
    ) -> IRDocument:
        doc = ir.model_copy(deep=True)
        instance_id = diff.subjects[0]
        if instance_id not in {o.id for o in doc.objects}:
            return doc
        instance = doc.get(instance_id)
        params = params or {}
        if strategy_id == "public_ip_authorized_networks":
            networks = _derive_authorized_networks(doc, instance)
            updated = _set_hint(instance, "public_ip", True)
            updated = _set_hint(updated, "authorized_networks", networks)
            return _replace_object(doc, updated)
        if strategy_id == "private_only":
            range_ = params.get("range", diff.context["psa_range"])
            prefix_length = params.get("prefix_length", diff.context["psa_prefix_length"])
            updated = _set_hint(instance, "private_network", diff.context["network"])
            updated = _set_hint(updated, "psa", {"range": range_, "prefix_length": prefix_length})
            return _replace_object(doc, updated)
        if strategy_id == "public_ip_allow_cidrs":
            updated = _set_hint(instance, "public_ip", True)
            updated = _set_hint(updated, "authorized_networks", [str(c) for c in params["cidrs"]])
            return _replace_object(doc, updated)
        if strategy_id == "manual":
            return _replace_object(
                doc, _exclude(instance, "DB-PUBLIC-ACCESS: public access requires manual review")
            )
        raise ValueError(f"unknown strategy {strategy_id!r}")


# ---- 5. DB-SG-FIREWALL -------------------------------------------------------


class DbSgFirewall(DifferenceRule):
    id = "DB-SG-FIREWALL"
    family = "database"
    title = "VPC firewall rules attached only to Cloud SQL instances have no effect there"
    impact = (
        "A private Cloud SQL instance lives in a Google-managed producer VPC reached over "
        "the PSA peering; a firewall policy attached only to database instances would "
        "otherwise generate a network-wide rule with an empty applies_to."
    )
    strategies = (
        Strategy(
            id="drop_with_note",
            title="Drop with a note",
            effect="excluded; VPC firewall rules do not govern Cloud SQL",
            risk="medium",
        ),
        Strategy(
            id="manual",
            title="Handle manually",
            effect="the same exclusion, acknowledged",
            risk="medium",
            manual=True,
        ),
    )
    default = "drop_with_note"

    def detect(self, ir: IRDocument) -> list[Difference]:
        """A policy is RDS-only when something is behind it, at least one member is a database
        instance, and no compute instance or launch template is (spec 4 §2.1.1).

        Membership is derived from forward references rather than read from a back-reference,
        so a policy shared with an EC2 instance can no longer look RDS-only and be dropped.
        """
        members = policy_members(ir)
        diffs = []
        for policy in sorted(ir.of_kind(FirewallPolicy), key=lambda p: p.id):
            behind = members.get(policy.id, set())
            if not behind:
                continue
            kinds = {member.split(":", 1)[0] for member in behind}
            if "database_instance" in kinds and not kinds & _NON_DATABASE_MEMBERS:
                diffs.append(self.difference([policy.id], applies_to=sorted(behind)))
        return diffs

    def apply(
        self,
        ir: IRDocument,
        diff: Difference,
        strategy_id: str,
        params: dict[str, Any] | None = None,
    ) -> IRDocument:
        doc = ir.model_copy(deep=True)
        policy_id = diff.subjects[0]
        if policy_id not in {o.id for o in doc.objects}:
            return doc
        if strategy_id not in ("drop_with_note", "manual"):
            raise ValueError(f"unknown strategy {strategy_id!r}")
        policy = doc.get(policy_id)
        return _replace_object(
            doc, _exclude(policy, "DB-SG-FIREWALL: VPC firewall rules do not govern Cloud SQL")
        )


# ---- 6. DB-MULTI-AZ ----------------------------------------------------------


class DbMultiAz(DifferenceRule):
    id = "DB-MULTI-AZ"
    family = "database"
    title = "AWS multi-AZ has two shapes; only one maps onto Cloud SQL"
    impact = (
        "A multi-AZ standby instance maps to Cloud SQL's regional availability type. A "
        "multi-AZ DB cluster (three instances, readable standbys) has no equivalent."
    )
    strategies = (
        Strategy(
            id="regional_ha",
            title="Use regional availability",
            effect="availability_type = REGIONAL",
            risk="low",
        ),
        Strategy(
            id="zonal",
            title="Use zonal availability",
            effect="availability_type = ZONAL; cheaper, no automatic failover",
            risk="medium",
        ),
        Strategy(id="manual", title="Handle manually", effect="excluded", risk="low", manual=True),
    )
    default = "regional_ha"

    def default_strategy(self, diff: Difference) -> str:
        return "manual" if diff.context["multi_az_cluster"] else "regional_ha"

    def allowed_strategies(self, diff: Difference) -> tuple[Strategy, ...]:
        ids = {"manual"} if diff.context["multi_az_cluster"] else {"regional_ha", "zonal"}
        return tuple(s for s in self.strategies if s.id in ids)

    def detect(self, ir: IRDocument) -> list[Difference]:
        diffs = []
        for instance in sorted(ir.of_kind(DatabaseInstance), key=lambda i: i.id):
            if instance.high_availability or instance.multi_az_cluster:
                diffs.append(
                    self.difference([instance.id], multi_az_cluster=instance.multi_az_cluster)
                )
        return diffs

    def apply(
        self,
        ir: IRDocument,
        diff: Difference,
        strategy_id: str,
        params: dict[str, Any] | None = None,
    ) -> IRDocument:
        doc = ir.model_copy(deep=True)
        instance_id = diff.subjects[0]
        if instance_id not in {o.id for o in doc.objects}:
            return doc
        allowed = {s.id for s in self.allowed_strategies(diff)}
        if strategy_id not in allowed:
            raise ValueError(f"unknown strategy {strategy_id!r}")
        instance = doc.get(instance_id)
        if strategy_id == "manual":
            return _replace_object(
                doc,
                _exclude(
                    instance, "DB-MULTI-AZ: multi-AZ DB clusters have no Cloud SQL equivalent"
                ),
            )
        availability = "REGIONAL" if strategy_id == "regional_ha" else "ZONAL"
        return _replace_object(doc, _set_hint(instance, "availability_type", availability))


# ---- 7. DB-STORAGE-AUTORESIZE -------------------------------------------------


class DbStorageAutoresize(DifferenceRule):
    id = "DB-STORAGE-AUTORESIZE"
    family = "database"
    title = "Cloud SQL enables storage autoresize by default; RDS does not"
    impact = (
        "Cloud SQL defaults storage autoresize on. Leaving it untouched would silently "
        "change behaviour for an RDS instance that had autoscaling off."
    )
    strategies = (
        Strategy(
            id="match_source",
            title="Match the source autoscaling setting",
            effect="autoresize on with the source's max size as the limit, or off if RDS "
            "storage autoscaling was off",
            risk="low",
        ),
        Strategy(
            id="enable_with_limit",
            title="Enable autoresize with a limit",
            effect="autoresize on, limited to the source's max size, or unlimited if none was set",
            risk="low",
        ),
    )
    default = "match_source"

    def detect(self, ir: IRDocument) -> list[Difference]:
        return [
            self.difference([instance.id], max_size_gib=instance.storage.max_size_gib)
            for instance in sorted(ir.of_kind(DatabaseInstance), key=lambda i: i.id)
        ]

    def apply(
        self,
        ir: IRDocument,
        diff: Difference,
        strategy_id: str,
        params: dict[str, Any] | None = None,
    ) -> IRDocument:
        doc = ir.model_copy(deep=True)
        instance_id = diff.subjects[0]
        if instance_id not in {o.id for o in doc.objects}:
            return doc
        instance = doc.get(instance_id)
        max_size = diff.context["max_size_gib"]
        if strategy_id == "match_source":
            autoresize = max_size is not None
            updated = _set_hint(instance, "disk_autoresize", autoresize)
            if autoresize:
                updated = _set_hint(updated, "disk_autoresize_limit", max_size)
            return _replace_object(doc, updated)
        if strategy_id == "enable_with_limit":
            limit = max_size if max_size is not None else 0
            updated = _set_hint(instance, "disk_autoresize", True)
            updated = _set_hint(updated, "disk_autoresize_limit", limit)
            return _replace_object(doc, updated)
        raise ValueError(f"unknown strategy {strategy_id!r}")


# ---- 8. DB-STORAGE-TYPE -------------------------------------------------------


class DbStorageType(DifferenceRule):
    id = "DB-STORAGE-TYPE"
    family = "database"
    title = "Provisioned IOPS/throughput storage has no Cloud SQL equivalent"
    impact = (
        "Cloud SQL scales IOPS with disk size rather than letting IOPS/throughput be "
        "provisioned directly; those figures are lost and small disks may be slower."
    )
    strategies = (
        Strategy(
            id="pd_ssd",
            title="Use PD_SSD",
            effect="disk_type = PD_SSD; provisioned IOPS/throughput are dropped",
            risk="medium",
        ),
        Strategy(id="manual", title="Handle manually", effect="excluded", risk="low", manual=True),
    )
    default = "pd_ssd"

    def detect(self, ir: IRDocument) -> list[Difference]:
        diffs = []
        for instance in sorted(ir.of_kind(DatabaseInstance), key=lambda i: i.id):
            storage = instance.storage
            provisioned = storage.type in ("io1", "io2") or (
                storage.type == "gp3"
                and (storage.iops is not None or storage.throughput_mibps is not None)
            )
            if provisioned:
                diffs.append(
                    self.difference(
                        [instance.id],
                        type=storage.type,
                        iops=storage.iops,
                        throughput_mibps=storage.throughput_mibps,
                    )
                )
        return diffs

    def apply(
        self,
        ir: IRDocument,
        diff: Difference,
        strategy_id: str,
        params: dict[str, Any] | None = None,
    ) -> IRDocument:
        doc = ir.model_copy(deep=True)
        instance_id = diff.subjects[0]
        if instance_id not in {o.id for o in doc.objects}:
            return doc
        instance = doc.get(instance_id)
        if strategy_id == "pd_ssd":
            return _replace_object(doc, _set_hint(instance, "disk_type", "PD_SSD"))
        if strategy_id == "manual":
            return _replace_object(
                doc,
                _exclude(
                    instance,
                    "DB-STORAGE-TYPE: provisioned IOPS/throughput have no Cloud SQL equivalent",
                ),
            )
        raise ValueError(f"unknown strategy {strategy_id!r}")


# ---- 9. DB-STORAGE-MINIMUM ----------------------------------------------------


class DbStorageMinimum(DifferenceRule):
    id = "DB-STORAGE-MINIMUM"
    family = "database"
    title = "Cloud SQL's minimum disk size is larger than some RDS instances"
    impact = f"Cloud SQL's minimum disk size is {MIN_DISK_GIB} GB."
    strategies = (
        Strategy(
            id="grow_to_minimum",
            title="Grow to the minimum",
            effect=f"disk_size = {MIN_DISK_GIB}",
            risk="none",
        ),
    )
    default = "grow_to_minimum"

    def detect(self, ir: IRDocument) -> list[Difference]:
        return [
            self.difference([instance.id], size_gib=instance.storage.size_gib)
            for instance in sorted(ir.of_kind(DatabaseInstance), key=lambda i: i.id)
            if instance.storage.size_gib < MIN_DISK_GIB
        ]

    def apply(
        self,
        ir: IRDocument,
        diff: Difference,
        strategy_id: str,
        params: dict[str, Any] | None = None,
    ) -> IRDocument:
        doc = ir.model_copy(deep=True)
        instance_id = diff.subjects[0]
        if instance_id not in {o.id for o in doc.objects}:
            return doc
        if strategy_id != "grow_to_minimum":
            raise ValueError(f"unknown strategy {strategy_id!r}")
        instance = doc.get(instance_id)
        return _replace_object(doc, _set_hint(instance, "disk_size", MIN_DISK_GIB))


# ---- 10. DB-PARAM-FLAGS -------------------------------------------------------


def _split_flags(engine: str, parameters: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    supported = SUPPORTED_FLAGS.get(engine, frozenset())
    kept: dict[str, str] = {}
    dropped: dict[str, str] = {}
    for key in sorted(parameters):
        value = parameters[key]
        if key in supported and not FORMULA.match(value):
            kept[key] = value
        else:
            dropped[key] = value
    return kept, dropped


class DbParamFlags(DifferenceRule):
    id = "DB-PARAM-FLAGS"
    family = "database"
    title = "AWS DB parameters translate onto a per-engine Cloud SQL flag allow list"
    impact = (
        "Only flags on Cloud SQL's per-engine supported list can be set; the rest, and "
        "every AWS formula value, are dropped and reported."
    )
    strategies = (
        Strategy(
            id="supported_flags_only",
            title="Translate supported flags, drop the rest",
            effect="database_flags gets the supported subset; the rest are dropped_parameters",
            risk="medium",
        ),
        Strategy(id="manual", title="Handle manually", effect="excluded", risk="low", manual=True),
    )
    default = "supported_flags_only"

    def detect(self, ir: IRDocument) -> list[Difference]:
        diffs = []
        for instance in sorted(ir.of_kind(DatabaseInstance), key=lambda i: i.id):
            if not instance.parameters:
                continue
            kept, dropped = _split_flags(instance.engine, instance.parameters)
            diffs.append(
                self.difference([instance.id], database_flags=kept, dropped_parameters=dropped)
            )
        return diffs

    def apply(
        self,
        ir: IRDocument,
        diff: Difference,
        strategy_id: str,
        params: dict[str, Any] | None = None,
    ) -> IRDocument:
        doc = ir.model_copy(deep=True)
        instance_id = diff.subjects[0]
        if instance_id not in {o.id for o in doc.objects}:
            return doc
        instance = doc.get(instance_id)
        if strategy_id == "supported_flags_only":
            merged_flags = {
                **instance.target_hints.get("database_flags", {}),
                **diff.context["database_flags"],
            }
            updated = _set_hint(instance, "database_flags", merged_flags)
            updated = _set_hint(
                updated, "dropped_parameters", dict(diff.context["dropped_parameters"])
            )
            return _replace_object(doc, updated)
        if strategy_id == "manual":
            return _replace_object(
                doc, _exclude(instance, "DB-PARAM-FLAGS: parameter group requires manual review")
            )
        raise ValueError(f"unknown strategy {strategy_id!r}")


# ---- shared helpers for rules 11-20 ----------------------------------------


def _window_start(window: str) -> str:
    """`"03:00-04:00"` -> `"03:00"`."""
    return window.split("-", 1)[0]


def _maintenance_day_and_hour(window: str) -> tuple[int, int]:
    """`"sun:05:30-sun:06:30"` -> `(7, 5)`; minutes are discarded."""
    day_name, hour, _minute = _window_start(window).split(":")
    return DAY_NUMBER[day_name], int(hour)


# ---- 11. DB-BACKUP-WINDOW ---------------------------------------------------


class DbBackupWindow(DifferenceRule):
    id = "DB-BACKUP-WINDOW"
    family = "database"
    title = "RDS backup windows and retention do not map directly onto Cloud SQL"
    impact = (
        "Cloud SQL takes a backup start time, not a window, and caps retained backups; "
        "an RDS instance with backups off has no automatic Cloud SQL equivalent."
    )
    strategies = (
        Strategy(
            id="nearest_equivalent",
            title="Use the nearest equivalent backup schedule",
            effect="backup.start_time from the window start; retained_backups clamped to "
            "Cloud SQL's limit",
            risk="low",
        ),
        Strategy(id="manual", title="Handle manually", effect="excluded", risk="low", manual=True),
        Strategy(
            id="no_backups",
            title="Match the source and disable backups",
            effect="backup.enabled = False",
            risk="high",
        ),
        Strategy(
            id="enable_default_backups",
            title="Enable Cloud SQL's default backup schedule",
            effect="backup.enabled = True, 7 retained backups at 03:00",
            risk="none",
        ),
    )
    default = "nearest_equivalent"

    def default_strategy(self, diff: Difference) -> str:
        return "nearest_equivalent" if diff.context["retention_days"] > 0 else "no_backups"

    def allowed_strategies(self, diff: Difference) -> tuple[Strategy, ...]:
        ids = (
            {"nearest_equivalent", "manual"}
            if diff.context["retention_days"] > 0
            else {"no_backups", "enable_default_backups"}
        )
        return tuple(s for s in self.strategies if s.id in ids)

    def detect(self, ir: IRDocument) -> list[Difference]:
        diffs = []
        for instance in sorted(ir.of_kind(DatabaseInstance), key=lambda i: i.id):
            window = instance.backup.window
            diffs.append(
                self.difference(
                    [instance.id],
                    retention_days=instance.backup.retention_days,
                    window=window,
                    window_defaulted=window is None,
                    backups_clamped=instance.backup.retention_days > MAX_RETAINED_BACKUPS,
                )
            )
        return diffs

    def apply(
        self,
        ir: IRDocument,
        diff: Difference,
        strategy_id: str,
        params: dict[str, Any] | None = None,
    ) -> IRDocument:
        doc = ir.model_copy(deep=True)
        instance_id = diff.subjects[0]
        if instance_id not in {o.id for o in doc.objects}:
            return doc
        allowed = {s.id for s in self.allowed_strategies(diff)}
        if strategy_id not in allowed:
            raise ValueError(f"unknown strategy {strategy_id!r}")
        instance = doc.get(instance_id)
        if strategy_id == "manual":
            return _replace_object(
                doc, _exclude(instance, "DB-BACKUP-WINDOW: backup schedule requires manual review")
            )
        if strategy_id == "no_backups":
            return _replace_object(doc, _set_hint(instance, "backup", {"enabled": False}))
        if strategy_id == "enable_default_backups":
            backup = {"enabled": True, "start_time": "03:00", "retained_backups": 7}
            return _replace_object(doc, _set_hint(instance, "backup", backup))
        # nearest_equivalent: a missing window (window_defaulted) still needs a start
        # time to report, so one is invented here rather than left unset.
        window = diff.context["window"] or "00:00-01:00"
        retained = min(diff.context["retention_days"], MAX_RETAINED_BACKUPS)
        backup = {
            "enabled": True,
            "start_time": _window_start(window),
            "retained_backups": retained,
        }
        return _replace_object(doc, _set_hint(instance, "backup", backup))


# ---- 12. DB-PITR -------------------------------------------------------------


class DbPitr(DifferenceRule):
    id = "DB-PITR"
    family = "database"
    title = "RDS transaction-log retention exceeds Cloud SQL's point-in-time recovery maximum"
    impact = (
        "Cloud SQL's point-in-time recovery keeps transaction logs for at most "
        f"{MAX_TRANSACTION_LOG_RETENTION_DAYS} days, shorter than RDS's up to 35."
    )
    strategies = (
        Strategy(
            id="enable",
            title="Enable point-in-time recovery",
            effect="pitr.enabled = True, log_retention_days clamped to Cloud SQL's maximum",
            risk="low",
        ),
        Strategy(
            id="disable",
            title="Disable point-in-time recovery",
            effect="pitr.enabled = False",
            risk="medium",
        ),
    )
    default = "enable"

    def detect(self, ir: IRDocument) -> list[Difference]:
        return [
            self.difference(
                [instance.id],
                retention_days=instance.backup.retention_days,
                log_retention_clamped=(
                    instance.backup.retention_days > MAX_TRANSACTION_LOG_RETENTION_DAYS
                ),
            )
            for instance in sorted(ir.of_kind(DatabaseInstance), key=lambda i: i.id)
            if instance.backup.retention_days > 0
        ]

    def apply(
        self,
        ir: IRDocument,
        diff: Difference,
        strategy_id: str,
        params: dict[str, Any] | None = None,
    ) -> IRDocument:
        doc = ir.model_copy(deep=True)
        instance_id = diff.subjects[0]
        if instance_id not in {o.id for o in doc.objects}:
            return doc
        instance = doc.get(instance_id)
        if strategy_id == "disable":
            return _replace_object(doc, _set_hint(instance, "pitr", {"enabled": False}))
        if strategy_id == "enable":
            retention_days = diff.context["retention_days"]
            log_retention_days = min(retention_days, MAX_TRANSACTION_LOG_RETENTION_DAYS)
            pitr = {"enabled": True, "log_retention_days": log_retention_days}
            return _replace_object(doc, _set_hint(instance, "pitr", pitr))
        raise ValueError(f"unknown strategy {strategy_id!r}")


# ---- 13. DB-MAINTENANCE-WINDOW -----------------------------------------------


class DbMaintenanceWindow(DifferenceRule):
    id = "DB-MAINTENANCE-WINDOW"
    family = "database"
    title = "RDS maintenance windows carry a range and minutes; Cloud SQL takes a day and hour"
    impact = (
        "Cloud SQL's maintenance window is a single day and hour with no minutes and no "
        "end time; the window start is rounded down to the hour."
    )
    strategies = (
        Strategy(
            id="nearest_hour",
            title="Use the window start, rounded to the hour",
            effect="maintenance_window = {day, hour} from the window start; minutes discarded",
            risk="low",
        ),
        Strategy(
            id="manual",
            title="Handle manually",
            effect="no maintenance window is generated",
            risk="low",
            manual=True,
        ),
    )
    default = "nearest_hour"

    def detect(self, ir: IRDocument) -> list[Difference]:
        return [
            self.difference([instance.id], window=instance.maintenance.window)
            for instance in sorted(ir.of_kind(DatabaseInstance), key=lambda i: i.id)
            if instance.maintenance.window
        ]

    def apply(
        self,
        ir: IRDocument,
        diff: Difference,
        strategy_id: str,
        params: dict[str, Any] | None = None,
    ) -> IRDocument:
        doc = ir.model_copy(deep=True)
        instance_id = diff.subjects[0]
        if instance_id not in {o.id for o in doc.objects}:
            return doc
        if strategy_id == "manual":
            return doc
        if strategy_id != "nearest_hour":
            raise ValueError(f"unknown strategy {strategy_id!r}")
        instance = doc.get(instance_id)
        day, hour = _maintenance_day_and_hour(diff.context["window"])
        updated = _set_hint(instance, "maintenance_window", {"day": day, "hour": hour})
        return _replace_object(doc, updated)


# ---- 14. DB-PORT --------------------------------------------------------------


class DbPort(DifferenceRule):
    id = "DB-PORT"
    family = "database"
    title = "Cloud SQL's engine port is fixed; RDS allows a custom port"
    impact = (
        "Cloud SQL always listens on the engine's default port; a custom RDS port means "
        "every client connection string changes."
    )
    strategies = (
        Strategy(
            id="default_port",
            title="Use Cloud SQL's fixed port",
            effect="no hints; the source port is recorded for the migration note",
            risk="medium",
        ),
        Strategy(id="manual", title="Handle manually", effect="excluded", risk="low", manual=True),
    )
    default = "default_port"

    def detect(self, ir: IRDocument) -> list[Difference]:
        diffs = []
        for instance in sorted(ir.of_kind(DatabaseInstance), key=lambda i: i.id):
            default_port = DEFAULT_PORT[instance.engine]
            if instance.network.port != default_port:
                diffs.append(
                    self.difference(
                        [instance.id], source_port=instance.network.port, default_port=default_port
                    )
                )
        return diffs

    def apply(
        self,
        ir: IRDocument,
        diff: Difference,
        strategy_id: str,
        params: dict[str, Any] | None = None,
    ) -> IRDocument:
        doc = ir.model_copy(deep=True)
        instance_id = diff.subjects[0]
        if instance_id not in {o.id for o in doc.objects}:
            return doc
        instance = doc.get(instance_id)
        if strategy_id == "default_port":
            return _replace_object(doc, instance)
        if strategy_id == "manual":
            return _replace_object(
                doc, _exclude(instance, "DB-PORT: custom port requires manual review")
            )
        raise ValueError(f"unknown strategy {strategy_id!r}")


# ---- 15. DB-KMS-CMEK ----------------------------------------------------------


class DbKmsCmek(DifferenceRule):
    id = "DB-KMS-CMEK"
    family = "database"
    title = "A customer-managed AWS KMS key has no Cloud SQL equivalent by default"
    impact = (
        "Cloud SQL encrypts at rest with Google-managed keys unless a Cloud KMS key is "
        "explicitly configured; the AWS key itself cannot be reused."
    )
    strategies = (
        Strategy(
            id="google_managed",
            title="Use Google-managed encryption",
            effect="no hints; still encrypted at rest, by Google-managed keys",
            risk="low",
        ),
        Strategy(id="manual", title="Handle manually", effect="excluded", risk="low", manual=True),
    )
    default = "google_managed"

    def detect(self, ir: IRDocument) -> list[Difference]:
        diffs = []
        for instance in sorted(ir.of_kind(DatabaseInstance), key=lambda i: i.id):
            if instance.storage.encrypted and instance.storage.kms_key:
                diffs.append(self.difference([instance.id], kms_key=instance.storage.kms_key))
        return diffs

    def apply(
        self,
        ir: IRDocument,
        diff: Difference,
        strategy_id: str,
        params: dict[str, Any] | None = None,
    ) -> IRDocument:
        doc = ir.model_copy(deep=True)
        instance_id = diff.subjects[0]
        if instance_id not in {o.id for o in doc.objects}:
            return doc
        instance = doc.get(instance_id)
        if strategy_id == "google_managed":
            return _replace_object(doc, instance)
        if strategy_id == "manual":
            return _replace_object(
                doc, _exclude(instance, "DB-KMS-CMEK: customer-managed key requires manual review")
            )
        raise ValueError(f"unknown strategy {strategy_id!r}")


# ---- 16. DB-IAM-AUTH ----------------------------------------------------------


class DbIamAuth(DifferenceRule):
    id = "DB-IAM-AUTH"
    family = "database"
    title = "AWS IAM database authentication does not translate; Cloud SQL has its own flag"
    impact = (
        "Cloud SQL's IAM database authentication is a per-instance flag; enabling it does "
        "not recreate the AWS-side IAM users or grants, which are invisible to this tool."
    )
    strategies = (
        Strategy(
            id="enable_flag",
            title="Enable Cloud SQL IAM authentication",
            effect="cloudsql_iam_authentication = 'on', merged into database_flags",
            risk="medium",
        ),
        Strategy(
            id="manual",
            title="Handle manually",
            effect="no hints; IAM authentication is not enabled",
            risk="low",
            manual=True,
        ),
    )
    default = "enable_flag"

    def detect(self, ir: IRDocument) -> list[Difference]:
        return [
            self.difference([instance.id])
            for instance in sorted(ir.of_kind(DatabaseInstance), key=lambda i: i.id)
            if instance.iam_auth
        ]

    def apply(
        self,
        ir: IRDocument,
        diff: Difference,
        strategy_id: str,
        params: dict[str, Any] | None = None,
    ) -> IRDocument:
        doc = ir.model_copy(deep=True)
        instance_id = diff.subjects[0]
        if instance_id not in {o.id for o in doc.objects}:
            return doc
        instance = doc.get(instance_id)
        if strategy_id == "manual":
            return _replace_object(doc, instance)
        if strategy_id != "enable_flag":
            raise ValueError(f"unknown strategy {strategy_id!r}")
        # Merge into whatever DB-PARAM-FLAGS (rule 10) already wrote, regardless of
        # which of the two rules ran first (spec §5 "rule interactions").
        merged_flags = {
            **instance.target_hints.get("database_flags", {}),
            "cloudsql_iam_authentication": "on",
        }
        return _replace_object(doc, _set_hint(instance, "database_flags", merged_flags))


# ---- 17. DB-LOG-EXPORTS -------------------------------------------------------


class DbLogExports(DifferenceRule):
    id = "DB-LOG-EXPORTS"
    family = "database"
    title = "RDS's per-log-type export selection has no Cloud SQL equivalent"
    impact = (
        "Cloud SQL sends logs to Cloud Logging by default; there is no per-log-type "
        "selection to translate, so this is informational only."
    )
    strategies = (
        Strategy(
            id="note_only",
            title="No change",
            effect="informational only; Cloud SQL logs to Cloud Logging by default",
            risk="none",
        ),
    )
    default = "note_only"

    def detect(self, ir: IRDocument) -> list[Difference]:
        return [
            self.difference([instance.id], log_exports=list(instance.log_exports))
            for instance in sorted(ir.of_kind(DatabaseInstance), key=lambda i: i.id)
            if instance.log_exports
        ]

    def apply(
        self,
        ir: IRDocument,
        diff: Difference,
        strategy_id: str,
        params: dict[str, Any] | None = None,
    ) -> IRDocument:
        doc = ir.model_copy(deep=True)
        instance_id = diff.subjects[0]
        if instance_id not in {o.id for o in doc.objects}:
            return doc
        if strategy_id != "note_only":
            raise ValueError(f"unknown strategy {strategy_id!r}")
        return _replace_object(doc, doc.get(instance_id))


# ---- 18. DB-REPLICA -----------------------------------------------------------


class DbReplica(DifferenceRule):
    id = "DB-REPLICA"
    family = "database"
    title = "A read replica needs its Cloud SQL primary named explicitly"
    impact = (
        "Cloud SQL replicas reference their primary by name; a replica of an "
        "unmigrated (external) primary has nothing to reference and cannot be created "
        "as a Cloud SQL replica."
    )
    strategies = (
        Strategy(
            id="cloud_sql_replica",
            title="Create as a Cloud SQL read replica",
            effect="master_instance set to the primary's IR id",
            risk="low",
        ),
        Strategy(id="manual", title="Handle manually", effect="excluded", risk="low", manual=True),
    )
    default = "cloud_sql_replica"

    def default_strategy(self, diff: Difference) -> str:
        return "manual" if diff.context["primary_id"] is None else "cloud_sql_replica"

    def allowed_strategies(self, diff: Difference) -> tuple[Strategy, ...]:
        ids = {"manual"} if diff.context["primary_id"] is None else {"cloud_sql_replica", "manual"}
        return tuple(s for s in self.strategies if s.id in ids)

    def detect(self, ir: IRDocument) -> list[Difference]:
        diffs = []
        for instance in sorted(ir.of_kind(DatabaseInstance), key=lambda i: i.id):
            if instance.role != "read_replica":
                continue
            replica_of = instance.replica_of
            is_external = replica_of.startswith("external:")
            primary_id = None if is_external else replica_of
            subjects = [instance.id] if is_external else [instance.id, primary_id]
            diffs.append(self.difference(subjects, replica_of=replica_of, primary_id=primary_id))
        return diffs

    def apply(
        self,
        ir: IRDocument,
        diff: Difference,
        strategy_id: str,
        params: dict[str, Any] | None = None,
    ) -> IRDocument:
        doc = ir.model_copy(deep=True)
        instance_id = diff.subjects[0]
        if instance_id not in {o.id for o in doc.objects}:
            return doc
        primary_id = diff.context["primary_id"]
        allowed = {s.id for s in self.allowed_strategies(diff)}
        if strategy_id not in allowed:
            raise ValueError(f"unknown strategy {strategy_id!r}")
        instance = doc.get(instance_id)
        if strategy_id == "manual":
            return _replace_object(
                doc,
                _exclude(
                    instance, "DB-REPLICA: replica of an external primary requires manual review"
                ),
            )
        return _replace_object(doc, _set_hint(instance, "master_instance", primary_id))


# ---- 19. DB-USERS --------------------------------------------------------------


class DbUsers(DifferenceRule):
    id = "DB-USERS"
    family = "database"
    title = "Only the master user is visible to Cloud SQL; every other user must be recreated"
    impact = (
        "Cloud SQL's control plane only sees the master user. Every other user, grant, "
        "schema and extension must be recreated after cutover; this rule exists so the "
        "user sees and approves that fact for every instance."
    )
    strategies = (
        Strategy(
            id="root_user_only",
            title="Acknowledge root-user-only visibility",
            effect="no change; recorded for review",
            risk="medium",
        ),
    )
    default = "root_user_only"

    def detect(self, ir: IRDocument) -> list[Difference]:
        return [
            self.difference([instance.id], master_username=instance.master_username)
            for instance in sorted(ir.of_kind(DatabaseInstance), key=lambda i: i.id)
        ]

    def apply(
        self,
        ir: IRDocument,
        diff: Difference,
        strategy_id: str,
        params: dict[str, Any] | None = None,
    ) -> IRDocument:
        doc = ir.model_copy(deep=True)
        instance_id = diff.subjects[0]
        if instance_id not in {o.id for o in doc.objects}:
            return doc
        if strategy_id != "root_user_only":
            raise ValueError(f"unknown strategy {strategy_id!r}")
        return _replace_object(doc, doc.get(instance_id))


# ---- 20. DB-NAME-REUSE ---------------------------------------------------------


class DbNameReuse(DifferenceRule):
    id = "DB-NAME-REUSE"
    family = "database"
    title = "A deleted Cloud SQL instance name cannot be reused for about a week"
    impact = (
        "Deleting and recreating a Cloud SQL instance under the same name fails for "
        "about a week after deletion; this matters when re-running a migration."
    )
    strategies = (Strategy(id="keep", title="No change", effect="informational only", risk="none"),)
    default = "keep"

    def detect(self, ir: IRDocument) -> list[Difference]:
        return [
            self.difference([instance.id], name=instance.name)
            for instance in sorted(ir.of_kind(DatabaseInstance), key=lambda i: i.id)
        ]

    def apply(
        self,
        ir: IRDocument,
        diff: Difference,
        strategy_id: str,
        params: dict[str, Any] | None = None,
    ) -> IRDocument:
        doc = ir.model_copy(deep=True)
        instance_id = diff.subjects[0]
        if instance_id not in {o.id for o in doc.objects}:
            return doc
        if strategy_id != "keep":
            raise ValueError(f"unknown strategy {strategy_id!r}")
        return _replace_object(doc, doc.get(instance_id))
