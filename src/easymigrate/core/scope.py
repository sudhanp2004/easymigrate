"""Migration scope: which scanned resources a migration covers.

A scan reads the whole region. Real accounts hold far more than one migration should touch — a
default VPC, other teams' instances, leftovers — and without a scope every one of them becomes
a difference to decide and a resource to create. The scope keeps:

1. **Top-level resources** (instances, Auto Scaling groups, databases, clusters, load balancers,
   buckets, unattached Elastic IPs) that pass every filter given:
   - `vpcs`: the resource sits in one of these VPCs. Resources that belong to no VPC (buckets,
     unattached Elastic IPs) are not affected by this filter; `tags` and `exclude` still apply.
   - `tags`: the resource carries one of these tags (`key` or `key=value`, matched as labels).
   - `exclude`: the resource is not named here (native id, IR id or name).
2. **Everything they depend on**, transitively: subnets, security groups, disks, templates,
   target groups, peered security groups.
3. **The networking of every VPC in play** (subnets, NAT, NACLs, security groups), because
   migrating something in a VPC means migrating the VPC it runs in.
4. **Children of kept resources** that point at their parent (node pools of a kept cluster).

IAM roles are always kept: the IAM report already restricts itself to roles kept resources use.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from easymigrate.ir.base import Finding, IntentObject, StrictModel
from easymigrate.ir.naming import clean_labels

ROOT_KINDS = frozenset(
    {
        "compute_instance",
        "instance_group",
        "database_instance",
        "kubernetes_cluster",
        "load_balancer",
        "storage_bucket",
        "external_address",
    }
)
NETWORKING_KINDS = frozenset({"network", "subnet", "firewall_policy", "nat", "network_acl"})
# Kinds that reference their parent rather than being referenced by it.
CHILD_KINDS = frozenset({"node_pool"})
ALWAYS_KEPT = frozenset({"iam_role"})


class Scope(StrictModel):
    vpcs: list[str] = []
    tags: list[str] = []
    exclude: list[str] = []

    def is_empty(self) -> bool:
        return not (self.vpcs or self.tags or self.exclude)


@dataclass
class ScopeResult:
    objects: list[IntentObject]
    findings: list[Finding] = field(default_factory=list)


def _matches(obj: IntentObject, names: set[str]) -> bool:
    return bool({obj.id, obj.source.id, obj.name} & names)


def _tag_match(obj: IntentObject, filters: list[str]) -> bool:
    for item in filters:
        key, sep, value = item.partition("=")
        wanted = clean_labels({key: value if sep else ""})
        if not wanted:
            continue
        ((label_key, label_value),) = wanted.items()
        if label_key in obj.labels and (not sep or obj.labels[label_key] == label_value):
            return True
    return False


def _parent(obj: IntentObject) -> str | None:
    return getattr(obj, "cluster", None)


def apply_scope(objects: list[IntentObject], scope: Scope | None) -> ScopeResult:
    if scope is None or scope.is_empty():
        return ScopeResult(objects=list(objects))
    by_id = {obj.id: obj for obj in objects}
    excluded = set(scope.exclude)

    def refs(obj_id: str) -> list[str]:
        return [r for r in by_id[obj_id].references() if r in by_id]

    def anchor(obj_id: str) -> str | None:
        """The network an object ultimately sits in, following references."""
        seen, queue = {obj_id}, deque([obj_id])
        while queue:
            current = queue.popleft()
            if by_id[current].kind == "network":
                return current
            for ref in refs(current):
                if ref not in seen:
                    seen.add(ref)
                    queue.append(ref)
        return None

    wanted_networks = {
        obj.id
        for obj in objects
        if obj.kind == "network" and scope.vpcs and _matches(obj, set(scope.vpcs))
    }
    excluded_networks = {
        obj.id for obj in objects if obj.kind == "network" and _matches(obj, excluded)
    }

    roots = []
    for obj in objects:
        if obj.kind not in ROOT_KINDS or _matches(obj, excluded):
            continue
        network = anchor(obj.id)
        if network in excluded_networks:
            continue
        if scope.vpcs and network is not None and network not in wanted_networks:
            continue
        if scope.tags and not _tag_match(obj, scope.tags):
            continue
        roots.append(obj.id)

    kept: set[str] = set()

    def keep(start: list[str]) -> None:
        queue = deque(start)
        while queue:
            current = queue.popleft()
            if current in kept:
                continue
            kept.add(current)
            queue.extend(refs(current))

    keep(roots)
    keep([obj.id for obj in objects if obj.kind in ALWAYS_KEPT])
    # With only exclusions, nothing narrows the networks: keep every one not excluded.
    if scope.vpcs or scope.tags:
        networks = {k for k in kept if by_id[k].kind == "network"} | wanted_networks
    else:
        networks = {o.id for o in objects if o.kind == "network"} - excluded_networks
    keep([obj.id for obj in objects if obj.kind in NETWORKING_KINDS and anchor(obj.id) in networks])
    keep([obj.id for obj in objects if obj.kind in CHILD_KINDS and _parent(obj) in kept])

    findings: list[Finding] = []
    blocked = sorted(obj.id for obj in objects if obj.id in kept and _matches(obj, excluded))
    if blocked:
        findings.append(
            Finding(
                level="info",
                code="SCOPE-EXCLUDE-IGNORED",
                message=(
                    "These were named in --exclude but kept, because something in scope depends "
                    f"on them: {', '.join(blocked)}."
                ),
            )
        )
    dropped = [obj for obj in objects if obj.id not in kept]
    if dropped:
        counts: dict[str, int] = {}
        for obj in dropped:
            counts[obj.kind] = counts.get(obj.kind, 0) + 1
        summary = ", ".join(f"{n} {kind}" for kind, n in sorted(counts.items()))
        findings.append(
            Finding(
                level="info",
                code="SCOPE-OUT-OF-SCOPE",
                message=(
                    f"{len(dropped)} scanned object(s) are outside the migration scope and were "
                    f"left out ({summary}): {', '.join(sorted(o.id for o in dropped))}."
                ),
            )
        )
    if scope.vpcs:
        unaffected = sorted(
            o.id for o in objects if o.id in kept and o.kind in ROOT_KINDS and anchor(o.id) is None
        )
        if unaffected:
            findings.append(
                Finding(
                    level="info",
                    code="SCOPE-NOT-IN-A-VPC",
                    message=(
                        "--vpc does not apply to resources that belong to no VPC, so these were "
                        f"kept; use --tag or --exclude to narrow them: {', '.join(unaffected)}."
                    ),
                )
            )
    known = {n for o in objects if o.kind == "network" for n in (o.id, o.source.id, o.name)}
    missing = sorted(set(scope.vpcs) - known)
    if missing:
        findings.append(
            Finding(
                level="scan_gap",
                code="SCOPE-VPC-NOT-FOUND",
                message=f"--vpc named VPCs that are not in this scan: {', '.join(missing)}.",
            )
        )
    return ScopeResult(objects=[o for o in objects if o.id in kept], findings=findings)
