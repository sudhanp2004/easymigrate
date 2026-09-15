"""Stage orchestration for `scan`, `analyze` and `resolve` (spec §2.2, §2.3, §4.2)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from easymigrate.core import registry
from easymigrate.core.plugin import SCAN_FILE, CollectContext, NormalizeContext, Plugin, Snapshot
from easymigrate.core.project import Project, SourceConfig
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
    DifferenceRuleError,
)
from easymigrate.ir.base import Finding, IntentObject, IRDocument

_SAMPLE_ARNS = 3


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
        except (ValueError, DifferenceRuleError):
            # e.g. a decision uses an unknown strategy; readiness_problems already
            # reports this, and resolve_ir cannot meaningfully run here.
            superseded = []

    return ResolveSummary(
        decisions=merged, dropped=dropped, problems=problems, superseded=superseded
    )
