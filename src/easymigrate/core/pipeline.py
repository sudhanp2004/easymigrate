"""Stage orchestration for `scan`, `analyze`, `resolve`, `plan` and `apply`
(spec §2.2, §2.3, §4.2, §5.4, §5.5)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from easymigrate import __version__
from easymigrate.core import registry
from easymigrate.core.errors import Declined, EasyMigrateError, NotReady, PlanStale
from easymigrate.core.plugin import (
    SCAN_FILE,
    CollectContext,
    GenerateContext,
    NormalizeContext,
    Plugin,
    Snapshot,
)
from easymigrate.core.project import Project, SourceConfig, TargetConfig
from easymigrate.core.regions import gcp_region_for
from easymigrate.core.report import build_report, write_report
from easymigrate.differences.decisions import (
    from_yaml,
    merge_decisions,
    readiness_problems,
    to_yaml,
)
from easymigrate.differences.engine import SupersededDecision, catalogue, detect, resolve_ir
from easymigrate.differences.model import (
    Decision,
    DecisionSet,
    Difference,
    DifferenceRule,
)
from easymigrate.ir.base import Finding, IntentObject, IRDocument

_SAMPLE_ARNS = 3
_STATE_BUCKET_SUFFIX = "-easymigrate-tfstate"


@dataclass(frozen=True)
class ScanSummary:
    account_id: str
    region: str
    files: list[str]
    findings: list[Finding]
    first_run: bool


def scan(
    project: Project,
    *,
    region: str,
    profile: str | None = None,
    role_arn: str | None = None,
    plugins: Sequence[Plugin] | None = None,
    source: Any = None,
) -> ScanSummary:
    first_run = project.init()
    plugins = registry.PLUGINS if plugins is None else plugins
    source = source if source is not None else registry.source_for("aws")
    connection = source.connect(region=region, profile=profile, role_arn=role_arn)
    account_id = source.account_id(connection)

    files: dict[str, Any] = {}
    findings: list[Finding] = []
    for plugin in plugins:
        ctx = CollectContext(plugin=plugin.name)
        files.update(plugin.collect_aws(connection, ctx))
        findings.extend(ctx.findings)
    findings.extend(_inventory_findings(source, connection, plugins))

    snapshot_dir = project.snapshot_dir("aws")
    if snapshot_dir.is_dir():
        for old in snapshot_dir.glob("*.json"):
            old.unlink()
    for name in sorted(files):
        project.write_json(snapshot_dir / f"{name}.json", files[name])
    project.write_json(
        snapshot_dir / SCAN_FILE,
        {
            "account_id": account_id,
            "region": region,
            "scanned_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "findings": [f.model_dump(mode="json") for f in findings],
        },
    )
    config = project.load_config()
    project.save_config(
        config.model_copy(
            update={"source": SourceConfig(cloud="aws", account_id=account_id, region=region)}
        )
    )
    return ScanSummary(account_id, region, sorted(files), findings, first_run)


def _inventory_findings(source: Any, connection: Any, plugins: Sequence[Plugin]) -> list[Finding]:
    arns_by_type, access_denied = source.inventory(connection)
    if access_denied:
        return [
            Finding(
                level="scan_gap",
                code="SCAN-ACCESS-DENIED",
                message=(
                    "Access denied calling GetResources; resources from unsupported services "
                    "are not listed."
                ),
                suggestion="Grant tag:GetResources",
            )
        ]
    supported: set[str] = set()
    for plugin in plugins:
        supported |= plugin.aws_resource_types
    findings = []
    for resource_type, arns in arns_by_type.items():
        if resource_type in supported:
            continue
        sample = ", ".join(arns[:_SAMPLE_ARNS])
        if len(arns) > _SAMPLE_ARNS:
            sample += f" and {len(arns) - _SAMPLE_ARNS} more"
        findings.append(
            Finding(
                level="unsupported",
                code="UNSUPPORTED-RESOURCE",
                message=(
                    f"{len(arns)} {resource_type} resource(s) found that EasyMigrate does not "
                    f"migrate yet: {sample}"
                ),
                subject=resource_type,
                suggestion=(
                    "Plan to migrate these by hand, or wait for a release that supports them."
                ),
            )
        )
    findings.append(
        Finding(
            level="info",
            code="INVENTORY-TAGGED-ONLY",
            message=(
                "Resources from unsupported services are found through the AWS tagging API, "
                "which only lists tagged resources; untagged ones may be missing."
            ),
        )
    )
    return findings


def analyze(
    project: Project, *, plugins: Sequence[Plugin] | None = None, strict: bool = False
) -> IRDocument:
    plugins = registry.PLUGINS if plugins is None else plugins
    snapshot_dir = project.snapshot_dir("aws")
    meta = project.read_json(snapshot_dir / SCAN_FILE, produced_by="scan")
    snapshot = Snapshot.from_dir(snapshot_dir)
    ctx = NormalizeContext(account_id=meta["account_id"], region=meta["region"], strict=strict)

    objects: list[IntentObject] = []
    for plugin in plugins:
        produced = plugin.normalize(snapshot, ctx)
        objects.extend(produced)
        ctx.objects.update({obj.id: obj for obj in produced})

    doc = IRDocument(
        source={
            "cloud": "aws",
            "account_id": meta["account_id"],
            "region": meta["region"],
            "scanned_at": meta["scanned_at"],
        },
        objects=objects,
        findings=[*(Finding.model_validate(f) for f in meta["findings"]), *ctx.findings],
    )
    project.write_ir(project.ir_path, doc)
    _write_differences(project, doc)
    return doc


def _write_differences(project: Project, doc: IRDocument) -> None:
    differences = detect(doc)
    rules_by_id = {rule.id: rule for rule in catalogue()}
    fired = sorted({diff.rule_id for diff in differences})
    project.write_json(
        project.differences_path,
        {
            "differences": [diff.model_dump(mode="json") for diff in differences],
            "rules": {
                rule_id: {
                    "title": rules_by_id[rule_id].title,
                    "impact": rules_by_id[rule_id].impact,
                    "strategies": [
                        s.model_dump(mode="json") for s in rules_by_id[rule_id].strategies
                    ],
                    "default": rules_by_id[rule_id].default,
                }
                for rule_id in fired
            },
        },
    )


def load_differences(project: Project) -> list[Difference]:
    data = project.read_json(project.differences_path, produced_by="analyze")
    return [Difference.model_validate(item) for item in data["differences"]]


@dataclass(frozen=True)
class ResolveSummary:
    decisions: DecisionSet
    dropped: list[str]
    problems: list[str]
    superseded: list[SupersededDecision] = field(default_factory=list)


def resolve(
    project: Project,
    *,
    choose: Callable[[dict[str, Any]], tuple[str, bool]] | None = None,
    accept_defaults: bool = False,
    acknowledge_manual: bool = False,
) -> ResolveSummary:
    ir_doc = project.read_ir(project.ir_path, produced_by="analyze")
    differences = load_differences(project)
    rules: tuple[DifferenceRule, ...] = catalogue()
    rules_by_id = {rule.id: rule for rule in rules}
    diffs_by_key = {diff.key: diff for diff in differences}

    existing = (
        from_yaml(project.decisions_path.read_text(encoding="utf-8"))
        if project.decisions_path.exists()
        else None
    )
    merged, dropped = merge_decisions(existing, differences, rules)

    if choose is not None and not accept_defaults:
        updated: list[Decision] = []
        for decision in merged.decisions:
            diff = diffs_by_key[decision.key]
            rule = rules_by_id[diff.rule_id]
            view = {
                "difference": diff,
                "rule": rule,
                "current_strategy": decision.strategy,
                "current_acknowledged": decision.acknowledged,
            }
            strategy_id, acknowledged = choose(view)
            decided_by = decision.decided_by if strategy_id == decision.strategy else "user"
            updated.append(
                Decision(
                    key=decision.key,
                    strategy=strategy_id,
                    decided_by=decided_by,
                    acknowledged=acknowledged,
                )
            )
        merged = merged.model_copy(update={"decisions": updated})

    if acknowledge_manual:
        acked: list[Decision] = []
        for decision in merged.decisions:
            diff = diffs_by_key[decision.key]
            rule = rules_by_id[diff.rule_id]
            try:
                strategy = rule.strategy(decision.strategy)
            except KeyError:
                strategy = None
            if strategy is not None and strategy.manual:
                decision = decision.model_copy(update={"acknowledged": True})
            acked.append(decision)
        merged = merged.model_copy(update={"decisions": acked, "acknowledged_in_bulk": True})

    project.write_text(project.decisions_path, to_yaml(merged))
    problems = readiness_problems(merged, differences, rules)

    superseded: list[SupersededDecision] = []
    merged_by_key = {decision.key: decision for decision in merged.decisions}
    can_resolve = all(diff.key in merged_by_key for diff in differences)
    if can_resolve:
        try:
            superseded = resolve_ir(ir_doc, differences, merged, rules).superseded
        except ValueError:
            # e.g. a decision uses an unknown strategy; readiness_problems already
            # reports this, and resolve_ir cannot meaningfully run here.
            superseded = []

    project.write_json(
        project.superseded_path,
        [entry.model_dump(mode="json") for entry in superseded],
    )

    return ResolveSummary(
        decisions=merged, dropped=dropped, problems=problems, superseded=superseded
    )


@dataclass(frozen=True)
class PlanSummary:
    add: int
    change: int
    destroy: int
    changed_or_destroyed: list[str]
    superseded: list[SupersededDecision]
    findings: list[Finding]
    plan_text_path: Path


@dataclass(frozen=True)
class ApplyResult:
    summary: PlanSummary
    output: list[str]


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _terraform_hashes(terraform_dir: Path) -> dict[str, str]:
    if not terraform_dir.is_dir():
        return {}
    return {tf.name: _hash_file(tf) for tf in sorted(terraform_dir.glob("*.tf"))}


def _state_hashes(project: Project) -> dict[str, Any]:
    """Hashes of every input `apply` must still match (spec §5.4 step 8, §5.5 step 1)."""
    hashes: dict[str, Any] = {}
    for name, path in (
        ("ir.json", project.ir_path),
        ("decisions.yaml", project.decisions_path),
        ("resolved_ir.json", project.resolved_ir_path),
    ):
        hashes[name] = _hash_file(path) if path.exists() else None
    hashes["terraform"] = _terraform_hashes(project.terraform_dir)
    hashes["tfplan"] = _hash_file(project.tfplan_path) if project.tfplan_path.exists() else None
    return hashes


def _resource_change_counts(show: dict[str, Any]) -> tuple[int, int, int, list[str]]:
    add = change = destroy = 0
    changed_or_destroyed: list[str] = []
    for rc in show.get("resource_changes", []):
        actions = rc.get("change", {}).get("actions", [])
        address = rc.get("address", "")
        if not actions or actions == ["no-op"]:
            continue
        if actions == ["create"]:
            add += 1
            continue
        destroyed = "delete" in actions
        created = "create" in actions
        if destroyed:
            destroy += 1
        if created and actions != ["create"]:
            add += 1
        if not destroyed and not created:
            change += 1
        if destroyed or (created and actions != ["create"]) or (not destroyed and not created):
            changed_or_destroyed.append(address)
    return add, change, destroy, changed_or_destroyed


def plan(
    project: Project,
    *,
    gcp_project: str,
    gcp_region: str | None = None,
    confirm_bucket: Callable[[str], bool],
    tofu: Any = None,
    gcp: Any = None,
) -> PlanSummary:
    """Generate GCP Terraform from the resolved IR and run `tofu plan` (spec §5.4)."""
    config = project.load_config()
    if config.source is None:
        raise EasyMigrateError("no scan found; run `easymigrate scan` first")

    region = gcp_region_for(config.source.region, override=gcp_region)

    differences = load_differences(project)
    rules: tuple[DifferenceRule, ...] = catalogue()
    decisions = (
        from_yaml(project.decisions_path.read_text(encoding="utf-8"))
        if project.decisions_path.exists()
        else None
    )
    problems = readiness_problems(decisions, differences, rules)
    if problems:
        raise NotReady(problems)
    assert decisions is not None  # readiness_problems() would have failed otherwise

    ir_doc = project.read_ir(project.ir_path, produced_by="analyze")
    resolved = resolve_ir(ir_doc, differences, decisions, rules)
    project.write_ir(project.resolved_ir_path, resolved.document)
    project.write_json(
        project.superseded_path, [entry.model_dump(mode="json") for entry in resolved.superseded]
    )

    gcp = gcp if gcp is not None else registry.gcp_target()
    if not gcp.credentials_ok():
        raise EasyMigrateError(
            "no GCP credentials found; run `gcloud auth application-default login`"
        )

    bucket = f"{gcp_project}{_STATE_BUCKET_SUFFIX}"
    ok = gcp.ensure_bucket(gcp_project, bucket, region, confirm_bucket)
    if not ok:
        raise Declined(f"declined creating the state bucket {bucket!r}")

    terraform_dir = project.terraform_dir
    if terraform_dir.is_dir():
        for old in terraform_dir.glob("*.tf"):
            old.unlink()
    terraform_dir.mkdir(parents=True, exist_ok=True)

    decided_by = {decision.key: decision.decided_by for decision in decisions.decisions}
    gen_findings: list[Finding] = []
    ctx = GenerateContext(
        project=gcp_project,
        region=region,
        version=__version__,
        decided_by=decided_by,
        findings=gen_findings,
    )
    for gen_plugin in registry.PLUGINS:
        hcl_file = gen_plugin.generate_gcp(resolved.document, ctx)
        project.write_text(terraform_dir / f"{gen_plugin.name}.tf", hcl_file.render())
    for name, content in registry.generate_boilerplate_tf(gcp_project, region, __version__).items():
        project.write_text(terraform_dir / name, content)

    tofu_obj = tofu
    if tofu_obj is None:
        binary = registry.ensure_tofu()
        tofu_obj = registry.new_tofu(binary, terraform_dir)
    tofu_obj.init(backend_config={"bucket": bucket, "prefix": "easymigrate"})
    plan_text = tofu_obj.plan(out=project.tfplan_path)
    project.write_text(project.plan_txt_path, plan_text)
    show = tofu_obj.show_json(project.tfplan_path)
    add, change, destroy, changed_or_destroyed = _resource_change_counts(show)

    lock = {
        "hashes": _state_hashes(project),
        "gcp_project": gcp_project,
        "gcp_region": region,
    }
    project.write_json(project.plan_lock_path, lock)

    project.save_config(
        config.model_copy(
            update={"target": TargetConfig(cloud="gcp", project=gcp_project, region=region)}
        )
    )

    all_findings = [*resolved.document.findings, *gen_findings]
    project.write_json(
        project.plan_summary_path,
        {
            "add": add,
            "change": change,
            "destroy": destroy,
            "changed_or_destroyed": changed_or_destroyed,
            "plan_text_path": str(project.plan_txt_path),
            "findings": [f.model_dump(mode="json") for f in all_findings],
        },
    )

    write_report(project, build_report(project))

    return PlanSummary(
        add=add,
        change=change,
        destroy=destroy,
        changed_or_destroyed=changed_or_destroyed,
        superseded=resolved.superseded,
        findings=all_findings,
        plan_text_path=project.plan_txt_path,
    )


def _plan_summary_from_disk(project: Project) -> PlanSummary:
    data = json.loads(project.plan_summary_path.read_text(encoding="utf-8"))
    superseded_raw = (
        json.loads(project.superseded_path.read_text(encoding="utf-8"))
        if project.superseded_path.exists()
        else []
    )
    return PlanSummary(
        add=data["add"],
        change=data["change"],
        destroy=data["destroy"],
        changed_or_destroyed=data["changed_or_destroyed"],
        superseded=[SupersededDecision.model_validate(s) for s in superseded_raw],
        findings=[Finding.model_validate(f) for f in data.get("findings", [])],
        plan_text_path=Path(data["plan_text_path"]),
    )


def apply(
    project: Project,
    *,
    confirm: Callable[[PlanSummary], bool],
    stream: Callable[[str], None],
    tofu: Any = None,
) -> ApplyResult:
    """Apply exactly the plan `plan()` produced, after explicit confirmation (spec §5.5)."""
    if not project.tfplan_path.exists() or not project.plan_lock_path.exists():
        raise PlanStale("no plan found; run `easymigrate plan` first")

    lock = json.loads(project.plan_lock_path.read_text(encoding="utf-8"))
    current_hashes = _state_hashes(project)
    if current_hashes != lock.get("hashes"):
        raise PlanStale(
            "the plan is stale (files changed since `plan`); run `easymigrate plan` again"
        )

    summary = _plan_summary_from_disk(project)
    if not confirm(summary):
        raise Declined("apply declined")

    tofu_obj = tofu
    if tofu_obj is None:
        binary = registry.ensure_tofu()
        tofu_obj = registry.new_tofu(binary, project.terraform_dir)

    output: list[str] = []

    def _capture(line: str) -> None:
        output.append(line)
        stream(line)

    try:
        tofu_obj.apply(project.tfplan_path, _capture)
    except EasyMigrateError as exc:
        raise EasyMigrateError(
            f"{exc}\nstate is safe in the bucket; re-run `easymigrate plan` then `apply`"
        ) from exc

    write_report(project, build_report(project))

    return ApplyResult(summary=summary, output=output)
