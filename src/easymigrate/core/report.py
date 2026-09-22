"""The migration report: terminal (Rich), markdown and json views (spec §6.1, plan 4 B1).

`build_report` reads whichever of ir.json, differences.json, decisions.yaml and
plan_summary.json exist on disk and assembles a `Report`. It is called after
`analyze`, `resolve` and `plan`, so earlier stages leave later sections empty rather
than failing.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
from dataclasses import dataclass, field
from typing import Any

from easymigrate.core.project import Project
from easymigrate.differences.decisions import from_yaml
from easymigrate.differences.engine import catalogue
from easymigrate.differences.model import DifferenceRule
from easymigrate.ir.base import Finding, IRDocument
from easymigrate.ir.compute import ComputeInstance, Disk
from easymigrate.ir.database import DatabaseInstance
from easymigrate.ir.kubernetes import KubernetesCluster, NodePool
from easymigrate.ir.network import FirewallPolicy

_SECTION_ORDER = [
    "Scan gaps",
    "Needs your action",
    "Decisions",
    "Not yet supported",
    "Translated",
]


@dataclass(frozen=True)
class DecisionRow:
    key: str
    strategy: str
    decided_by: str
    risk: str


@dataclass(frozen=True)
class DatabaseRow:
    """What `plan` decided for one Cloud SQL instance, in the user's terms (spec §7)."""

    id: str
    tier: str
    tier_source: str
    connectivity: str
    open_to_world: bool
    backups: str
    pitr: str
    dropped_parameters: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tier": self.tier,
            "tier_source": self.tier_source,
            "connectivity": self.connectivity,
            "open_to_world": self.open_to_world,
            "backups": self.backups,
            "pitr": self.pitr,
            "dropped_parameters": dict(self.dropped_parameters),
        }


@dataclass(frozen=True)
class ComputeRow:
    """What `plan` decided for one Compute Engine instance, in the user's terms (spec 4 §7)."""

    id: str
    machine_type: str
    machine_source: str
    image: str
    ami: str
    firewall_policies: list[str]
    reachability: str
    empty_disks: list[str]
    service_account_note: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "machine_type": self.machine_type,
            "machine_source": self.machine_source,
            "image": self.image,
            "ami": self.ami,
            "firewall_policies": list(self.firewall_policies),
            "reachability": self.reachability,
            "empty_disks": list(self.empty_disks),
            "service_account_note": self.service_account_note,
        }


@dataclass(frozen=True)
class KubernetesRow:
    """What `plan` decided for one GKE cluster, in the user's terms (spec 5 §7)."""

    id: str
    version: str
    endpoint: str
    ranges: str
    node_pools: list[str]
    iam_bindings: list[str]
    access_entries: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "endpoint": self.endpoint,
            "ranges": self.ranges,
            "node_pools": list(self.node_pools),
            "iam_bindings": list(self.iam_bindings),
            "access_entries": list(self.access_entries),
        }


@dataclass(frozen=True)
class PlanSummaryView:
    add: int
    change: int
    destroy: int
    changed_or_destroyed: list[str]
    plan_text_path: str
    findings: list[Finding] = field(default_factory=list)
    # sha256 of plan.lock.json (spec §5): the UI's Apply page must send this back
    # verbatim as `plan_fingerprint`, so a page that reviewed one plan cannot approve a
    # different one that was regenerated since. Computed here, not read from a separate
    # endpoint, because the report is already "what the user reads to decide".
    fingerprint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "add": self.add,
            "change": self.change,
            "destroy": self.destroy,
            "changed_or_destroyed": self.changed_or_destroyed,
            "plan_text_path": self.plan_text_path,
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True)
class Report:
    source: str | None
    target: str | None
    coverage: dict[str, int]
    scan_gaps: list[Finding] = field(default_factory=list)
    needs_action: list[str] = field(default_factory=list)
    decisions: list[DecisionRow] = field(default_factory=list)
    not_supported: list[Finding] = field(default_factory=list)
    translated: dict[str, int] = field(default_factory=dict)
    info: list[Finding] = field(default_factory=list)
    databases: list[DatabaseRow] = field(default_factory=list)
    compute: list[ComputeRow] = field(default_factory=list)
    kubernetes: list[KubernetesRow] = field(default_factory=list)
    plan: PlanSummaryView | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "coverage": self.coverage,
            "scan_gaps": [f.model_dump(mode="json") for f in self.scan_gaps],
            "needs_action": list(self.needs_action),
            "decisions": [
                {"key": d.key, "strategy": d.strategy, "decided_by": d.decided_by, "risk": d.risk}
                for d in self.decisions
            ],
            "not_supported": [f.model_dump(mode="json") for f in self.not_supported],
            "translated": self.translated,
            "info": [f.model_dump(mode="json") for f in self.info],
            "databases": [d.to_dict() for d in self.databases],
            "compute": [c.to_dict() for c in self.compute],
            "kubernetes": [k.to_dict() for k in self.kubernetes],
            "plan": self.plan.to_dict() if self.plan else None,
        }

    def to_markdown(self) -> str:
        lines: list[str] = []
        lines.append(f"# EasyMigrate report: {self.source or '?'} → {self.target or '?'}")
        lines.append("")
        cov = self.coverage
        lines.append(
            f"Coverage: {cov.get('found', 0)} found, {cov.get('translated', 0)} translated, "
            f"{cov.get('decided', 0)} handled by decision, {cov.get('needs_action', 0)} need "
            f"your action, {cov.get('not_supported', 0)} not yet supported."
        )
        lines.append("")

        lines.append("## Scan gaps")
        if self.scan_gaps:
            for f in self.scan_gaps:
                lines.append(
                    f"- {f.code}: {f.message}" + (f" → {f.suggestion}" if f.suggestion else "")
                )
        else:
            lines.append("None.")
        lines.append("")

        lines.append("## Needs your action")
        if self.needs_action:
            for item in self.needs_action:
                lines.append(f"- {item}")
        else:
            lines.append("Nothing needs your action.")
        lines.append("")

        lines.append("## Decisions")
        if self.decisions:
            lines.append("| Key | Strategy | Default/User | Risk |")
            lines.append("|---|---|---|---|")
            for d in self.decisions:
                lines.append(f"| {d.key} | {d.strategy} | {d.decided_by} | {d.risk} |")
        else:
            lines.append("No decisions yet.")
        lines.append("")

        lines.append("## Not yet supported")
        if self.not_supported:
            for f in self.not_supported:
                lines.append(f"- {f.code}: {f.message}")
        else:
            lines.append("None.")
        lines.append("")

        lines.append("## Translated")
        if self.translated:
            for kind in sorted(self.translated):
                lines.append(f"- {kind}: {self.translated[kind]}")
        else:
            lines.append("Nothing translated yet.")
        lines.append("")

        if self.databases:
            lines.append("## Databases")
            for db in self.databases:
                lines.append(f"### {db.id}")
                lines.append(f"- Tier: {db.tier} (from {db.tier_source})")
                lines.append(f"- Connectivity: {db.connectivity}")
                if db.open_to_world:
                    lines.append(
                        "- **This database is reachable from the whole internet "
                        "(0.0.0.0/0), as it was on AWS.**"
                    )
                lines.append(f"- Backups: {db.backups}")
                lines.append(f"- Point-in-time recovery: {db.pitr}")
                if db.dropped_parameters:
                    lines.append("- Parameters with no Cloud SQL equivalent, dropped:")
                    for name in sorted(db.dropped_parameters):
                        lines.append(f"  - {name} = {db.dropped_parameters[name]}")
            lines.append("")
            lines.append(
                "The database password you supplied is stored in the Terraform state in your "
                "GCS bucket and in .easymigrate/terraform/secrets.auto.tfvars.json. Rotate it "
                "after cutover if that is not acceptable."
            )
            lines.append(
                "Only the master user was migrated. Other database users, their grants, "
                "schemas and extensions live inside the database itself, which a read-only "
                "AWS scan cannot see \u2014 recreate them after cutover."
            )
            lines.append("")

        if self.compute:
            lines.append("## Compute")
            for vm in self.compute:
                lines.append(f"### {vm.id}")
                lines.append(f"- Machine type: {vm.machine_type} (from {vm.machine_source})")
                lines.append(f"- Boots: {vm.image}; AMI {vm.ami} was not migrated")
                if vm.firewall_policies:
                    lines.append(
                        "- Network tags from security groups: " + ", ".join(vm.firewall_policies)
                    )
                else:
                    lines.append("- Network tags: none; no migrated firewall rule applies to it")
                lines.append(f"- Reachability: {vm.reachability}")
                for disk in vm.empty_disks:
                    lines.append(f"- Disk {disk} is created empty")
                if vm.service_account_note:
                    lines.append(f"- {vm.service_account_note}")
            lines.append("")
            lines.append(
                "No disk contents, installed software or running workload were migrated. Every "
                "instance boots as a blank machine of the right shape on the right network."
            )
            lines.append("")

        if self.kubernetes:
            lines.append("## Kubernetes")
            for cluster in self.kubernetes:
                lines.append(f"### {cluster.id}")
                lines.append(f"- Version: {cluster.version}")
                lines.append(f"- API endpoint: {cluster.endpoint}")
                lines.append(f"- Pod and service ranges: {cluster.ranges}")
                for pool in cluster.node_pools:
                    lines.append(f"- Node pool {pool}")
                if cluster.iam_bindings:
                    lines.append(
                        "- IAM for pods to recreate as Workload Identity bindings: "
                        + ", ".join(cluster.iam_bindings)
                    )
                if cluster.access_entries:
                    lines.append(
                        "- Access to re-grant (was an EKS access entry): "
                        + ", ".join(cluster.access_entries)
                    )
            lines.append("")
            lines.append(
                "Every cluster is created empty. No workloads, manifests, secrets, custom "
                "resources, Helm releases or persistent volumes were read or migrated; redeploy "
                "them from their source of truth."
            )
            lines.append("")

        if self.plan is not None:
            lines.append("## Plan summary")
            lines.append(
                f"- add: {self.plan.add}, change: {self.plan.change}, destroy: {self.plan.destroy}"
            )
            if self.plan.changed_or_destroyed:
                lines.append("- changed or destroyed:")
                for addr in self.plan.changed_or_destroyed:
                    lines.append(f"  - {addr}")
            lines.append(f"- plan text: {self.plan.plan_text_path}")
            lines.append("")

        if self.info:
            lines.append("## Notes")
            for f in self.info:
                lines.append(f"- {f.code}: {f.message}")
            lines.append("")

        return "\n".join(lines).rstrip() + "\n"


def _load_ir(project: Project) -> IRDocument | None:
    if not project.ir_path.exists():
        return None
    return project.read_ir(project.ir_path, produced_by="analyze")


def _load_decisions(project: Project):
    if not project.decisions_path.exists():
        return None
    return from_yaml(project.decisions_path.read_text(encoding="utf-8"))


def _load_plan_summary(project: Project) -> PlanSummaryView | None:
    if not project.plan_summary_path.exists():
        return None
    data = json.loads(project.plan_summary_path.read_text(encoding="utf-8"))
    return PlanSummaryView(
        add=data["add"],
        change=data["change"],
        destroy=data["destroy"],
        changed_or_destroyed=data["changed_or_destroyed"],
        plan_text_path=data["plan_text_path"],
        findings=[Finding.model_validate(f) for f in data.get("findings", [])],
        fingerprint=_plan_fingerprint(project),
    )


def _plan_fingerprint(project: Project) -> str | None:
    if not project.plan_lock_path.exists():
        return None
    return hashlib.sha256(project.plan_lock_path.read_bytes()).hexdigest()


def _load_superseded(project: Project) -> list[dict[str, Any]]:
    if not project.superseded_path.exists():
        return []
    return json.loads(project.superseded_path.read_text(encoding="utf-8"))


def _load_resolved_ir(project: Project) -> IRDocument | None:
    if not project.resolved_ir_path.exists():
        return None
    return project.read_ir(project.resolved_ir_path, produced_by="plan")


def _is_world_cidr(cidr: str) -> bool:
    """True for a CIDR that covers the whole IPv4 or IPv6 address space (I4). An exact
    match on `"0.0.0.0/0"` misses `::/0`, which AWS's normalized `Ipv6Ranges` produce
    just as readily and which is exactly as open to the world."""
    try:
        network = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return False
    return network.prefixlen == 0


def _connectivity(instance: DatabaseInstance) -> tuple[str, bool]:
    """How the migrated instance will be reachable, and whether that is from anywhere."""
    if instance.excluded is not None:
        return f"not migrated \u2014 {instance.excluded}", False
    hints = instance.target_hints
    psa = hints.get("psa")
    if hints.get("private_network") and psa:
        network = str(hints["private_network"]).split(":", 1)[-1]
        return (
            f"private IP only, over the {psa['range']}/{psa['prefix_length']} private "
            f"services access range on {network}",
            False,
        )
    if hints.get("public_ip"):
        networks = list(hints.get("authorized_networks") or [])
        open_to_world = any(_is_world_cidr(cidr) for cidr in networks)
        listed = ", ".join(networks) if networks else "no authorized networks, so unreachable"
        return f"public IP, reachable from {listed}", open_to_world
    return "not generated: no connectivity decision was applied", False


def _backups(instance: DatabaseInstance) -> str:
    if instance.role == "read_replica":
        return (
            "inherited from the primary; a Cloud SQL replica has no backup configuration of its own"
        )
    backup = instance.target_hints.get("backup")
    if not backup or not backup.get("enabled"):
        return "no automated backups, matching the source"
    retained = backup.get("retained_backups")
    text = f"{retained} backups retained, taken daily at {backup.get('start_time')} UTC"
    if instance.backup.retention_days and retained != instance.backup.retention_days:
        text += (
            f" (clamped from the source's {instance.backup.retention_days} days to what "
            "Cloud SQL allows)"
        )
    return text


def _pitr(instance: DatabaseInstance) -> str:
    if instance.role == "read_replica":
        return (
            "inherited from the primary; a Cloud SQL replica has no PITR configuration of its own"
        )
    pitr = instance.target_hints.get("pitr")
    if not pitr or not pitr.get("enabled"):
        return "off"
    days = pitr.get("log_retention_days")
    text = f"on, keeping {days} days of transaction logs"
    if instance.backup.retention_days and days != instance.backup.retention_days:
        text += (
            f" (the source kept {instance.backup.retention_days} days; Cloud SQL's maximum "
            "is shorter)"
        )
    return text


def _database_rows(resolved: IRDocument | None) -> list[DatabaseRow]:
    if resolved is None:
        return []
    rows: list[DatabaseRow] = []
    for instance in resolved.of_kind(DatabaseInstance):
        compute = instance.compute
        if compute.vcpus is not None and compute.memory_mib is not None:
            source = f"{compute.class_name}, {compute.vcpus} vCPU, {compute.memory_mib} MiB"
        else:
            source = f"{compute.class_name}, size unknown"
        connectivity, open_to_world = _connectivity(instance)
        rows.append(
            DatabaseRow(
                id=instance.id,
                tier=str(instance.target_hints.get("tier", "not decided")),
                tier_source=source,
                connectivity=connectivity,
                open_to_world=open_to_world,
                backups=_backups(instance),
                pitr=_pitr(instance),
                dropped_parameters=dict(instance.target_hints.get("dropped_parameters") or {}),
            )
        )
    return rows


def _compute_rows(resolved: IRDocument | None) -> list[ComputeRow]:
    if resolved is None:
        return []
    disks = {disk.id: disk for disk in resolved.of_kind(Disk)}
    live_policies = {
        policy.id: policy.name
        for policy in resolved.of_kind(FirewallPolicy)
        if policy.excluded is None
    }
    rows: list[ComputeRow] = []
    for instance in sorted(resolved.of_kind(ComputeInstance), key=lambda o: o.id):
        if instance.excluded is not None or instance.eks_cluster:
            continue
        hints = instance.target_hints
        if instance.vcpus is not None and instance.memory_mib is not None:
            source = f"{instance.instance_type}, {instance.vcpus} vCPU, {instance.memory_mib} MiB"
        else:
            source = f"{instance.instance_type}, size unknown"
        delta = hints.get("memory_delta_mib")
        if delta:
            source += f"; {delta:+d} MiB memory"
        image = hints.get("boot_image")
        external = hints.get("external", "none")
        reachability = {
            "none": "no external IP; reachable only from inside the VPC",
            "ephemeral": "reachable from the internet on a new ephemeral IP",
            "static": "reachable from the internet on a newly reserved static IP",
        }.get(external, external)
        rows.append(
            ComputeRow(
                id=instance.id,
                machine_type=str(hints.get("machine_type", "not decided")),
                machine_source=source,
                image=f"{image['project']}/{image['family']}" if image else "not decided",
                ami=instance.image,
                firewall_policies=sorted(
                    live_policies[p] for p in instance.firewall_policies if p in live_policies
                ),
                reachability=reachability,
                empty_disks=sorted(
                    disk_id
                    for disk_id in instance.disks
                    if disk_id in disks
                    and not disks[disk_id].boot
                    and not disks[disk_id].instance_store
                    and disks[disk_id].excluded is None
                ),
                service_account_note=(
                    f"No service account attached; the AWS role {instance.iam_profile} "
                    "was not translated"
                    if instance.iam_profile
                    else None
                ),
            )
        )
    return rows


def _kubernetes_rows(resolved: IRDocument | None) -> list[KubernetesRow]:
    if resolved is None:
        return []
    rows: list[KubernetesRow] = []
    pools = sorted(resolved.of_kind(NodePool), key=lambda p: p.id)
    for cluster in sorted(resolved.of_kind(KubernetesCluster), key=lambda c: c.id):
        if cluster.excluded is not None:
            continue
        hints = cluster.target_hints
        if hints.get("release_channel") == "REGULAR":
            version = "GKE Regular release channel (auto-upgrading)"
        else:
            version = f"{hints.get('version', cluster.version)}, pinned (EKS ran {cluster.version})"
        endpoint_hint = hints.get("endpoint") or {}
        if endpoint_hint.get("private_endpoint"):
            endpoint = "private, reachable only from inside the VPC"
        elif endpoint_hint.get("authorized_networks") is None:
            endpoint = "public and open to every address, as it was on EKS"
        else:
            endpoint = "public, limited to " + ", ".join(endpoint_hint["authorized_networks"])
        subnet_ranges = {}
        if hints.get("subnet"):
            subnet = next((o for o in resolved.objects if o.id == hints["subnet"]), None)
            if subnet is not None:
                subnet_ranges = {
                    e["name"]: e["cidr"] for e in subnet.target_hints.get("secondary_ranges", [])
                }
        ranges = (
            f"pods {subnet_ranges.get(hints.get('pods_range'), '?')}, services "
            f"{subnet_ranges.get(hints.get('services_range'), '?')} on {hints.get('subnet', '?')}"
        )
        pool_lines = []
        for pool in pools:
            if pool.cluster != cluster.id:
                continue
            if pool.excluded is not None:
                pool_lines.append(f"{pool.id}: not created ({pool.excluded})")
                continue
            ph = pool.target_hints
            scaling = ph.get("autoscaling")
            zones = len(ph.get("zone_letters") or [])
            if scaling:
                size = (
                    f"autoscaling {scaling['total_min']}–{scaling['total_max']} nodes in total, "
                    f"starting at {scaling['initial_per_zone']} per zone across {zones} zones"
                )
            else:
                size = f"{ph.get('node_count_per_zone', '?')} per zone across {zones} zones"
            carried = (
                "; custom AMI and bootstrap not carried" if pool.origin == "self_managed" else ""
            )
            pool_lines.append(
                f"{pool.id}: {ph.get('machine_type', '?')} on {ph.get('image_type', '?')}, "
                f"{size}{carried}"
            )
        if hints.get("default_pool"):
            spec = hints["default_pool"]
            pool_lines.append(
                f"added for Fargate pods: {spec['machine_type']}, {spec['nodes_per_zone']} per zone"
            )
        rows.append(
            KubernetesRow(
                id=cluster.id,
                version=version,
                endpoint=endpoint,
                ranges=ranges,
                node_pools=pool_lines,
                iam_bindings=[f"{p.namespace}/{p.service_account}" for p in cluster.pod_identities]
                or (
                    ["IRSA service accounts (OIDC provider was enabled)"]
                    if cluster.oidc_issuer
                    else []
                ),
                access_entries=list(cluster.access_entries),
            )
        )
    return rows


def build_report(project: Project) -> Report:
    config = project.load_config() if project.config_path.exists() else None
    source = config.source.cloud if config and config.source else None
    target = config.target.cloud if config and config.target else None

    ir = _load_ir(project)
    decisions = _load_decisions(project)
    plan_summary = _load_plan_summary(project)
    resolved = _load_resolved_ir(project)
    databases = _database_rows(resolved)
    compute = _compute_rows(resolved)
    kubernetes = _kubernetes_rows(resolved)
    superseded = _load_superseded(project)

    rules_by_id: dict[str, DifferenceRule] = {rule.id: rule for rule in catalogue()}

    scan_gaps: list[Finding] = []
    not_supported: list[Finding] = []
    info: list[Finding] = []
    ir_findings = list(ir.findings) if ir is not None else []
    for finding in ir_findings:
        if finding.level == "scan_gap":
            scan_gaps.append(finding)
        elif finding.level == "unsupported":
            not_supported.append(finding)
        elif finding.level == "info":
            info.append(finding)

    # plan_summary.json's findings are resolved-IR findings (identical to ir.findings;
    # resolving differences never adds findings) plus generation findings (e.g.
    # GEN-RENAMED, GEN-NAT-UNUSED) that only plan() produces. Fold whichever of those are
    # new here so they are not dropped silently (spec D2).
    if plan_summary is not None:
        for finding in plan_summary.findings:
            if finding in ir_findings:
                continue
            if finding.level == "scan_gap":
                scan_gaps.append(finding)
            elif finding.level == "unsupported":
                not_supported.append(finding)
            elif finding.level == "info":
                info.append(finding)

    decision_rows: list[DecisionRow] = []
    needs_action: list[str] = []
    if decisions is not None:
        diffs_by_key = _load_differences_by_key(project)
        for decision in decisions.decisions:
            diff = diffs_by_key.get(decision.key)
            rule = rules_by_id.get(diff.rule_id) if diff else None
            try:
                strategy = rule.strategy(decision.strategy) if rule else None
            except KeyError:
                strategy = None
            if strategy is not None and rule is not None and diff is not None:
                risk = rule.risk_for(diff, decision.strategy)
            else:
                risk = "none"
            decision_rows.append(
                DecisionRow(
                    key=decision.key,
                    strategy=decision.strategy,
                    decided_by=decision.decided_by,
                    risk=risk,
                )
            )
            if strategy is not None and strategy.manual:
                state = "acknowledged" if decision.acknowledged else "needs acknowledgement"
                needs_action.append(
                    f"{decision.key}: manual strategy {decision.strategy} ({state})"
                )

    if ir is not None:
        for obj in ir.objects:
            if obj.excluded is not None:
                needs_action.append(f"{obj.id} excluded: {obj.excluded}")

    for entry in superseded:
        needs_action.append(entry["message"])

    translated: dict[str, int] = {}
    if ir is not None:
        for obj in ir.objects:
            if obj.excluded is None:
                translated[obj.kind] = translated.get(obj.kind, 0) + 1

    found = len(ir.objects) if ir is not None else 0
    translated_count = sum(translated.values())
    decided_count = len(decision_rows)
    needs_action_count = len(needs_action)
    not_supported_count = len(not_supported)
    coverage = {
        "found": found,
        "translated": translated_count,
        "decided": decided_count,
        "needs_action": needs_action_count,
        "not_supported": not_supported_count,
    }

    return Report(
        source=source,
        target=target,
        coverage=coverage,
        scan_gaps=scan_gaps,
        needs_action=needs_action,
        decisions=decision_rows,
        not_supported=not_supported,
        translated=translated,
        info=info,
        databases=databases,
        compute=compute,
        kubernetes=kubernetes,
        plan=plan_summary,
    )


def _load_differences_by_key(project: Project) -> dict[str, Any]:
    if not project.differences_path.exists():
        return {}
    from easymigrate.differences.model import Difference

    data = json.loads(project.differences_path.read_text(encoding="utf-8"))
    return {item["key"]: Difference.model_validate(item) for item in data["differences"]}


def write_report(project: Project, report: Report) -> None:
    project.write_text(project.report_md_path, report.to_markdown())
    project.write_json(project.report_json_path, report.to_dict())
