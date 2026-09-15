"""Stage orchestration for `scan` and `analyze` (spec §2.2, §2.3)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from easymigrate.core import registry
from easymigrate.core.plugin import SCAN_FILE, CollectContext, NormalizeContext, Plugin, Snapshot
from easymigrate.core.project import Project, SourceConfig
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
    return doc
