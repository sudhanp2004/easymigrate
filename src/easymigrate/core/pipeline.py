"""Stage orchestration for `scan`, `analyze`, `resolve`, `plan` and `apply`
(spec §2.2, §2.3, §4.2, §5.4, §5.5)."""

from __future__ import annotations

import hashlib
import json
import os
import re
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
from easymigrate.ir.database import DatabaseInstance

_SAMPLE_ARNS = 3
_STATE_BUCKET_SUFFIX = "-easymigrate-tfstate"
_DB_PASSWORD_ENV_PREFIX = "EASYMIGRATE_DB_PASSWORD_"
_DB_PASSWORD_MAX_LEN = 100


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
    managed: list[tuple[str, re.Pattern[str]]] = []
    for plugin in plugins:
        supported |= plugin.aws_resource_types
        managed += [
            (kind, re.compile(pattern)) for kind, pattern in getattr(plugin, "managed_arns", ())
        ]
    findings = []
    for resource_type, all_arns in arns_by_type.items():
        if resource_type in supported:
            continue
        arns = [
            arn
            for arn in all_arns
            if not any(kind == resource_type and pattern.search(arn) for kind, pattern in managed)
        ]
        if not arns:
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


def check_plugin_order(plugins: Sequence[Plugin]) -> None:
    """Every plugin's `requires` must name a plugin earlier in the sequence (spec 4 §2.2).

    Validates the given order; it neither reorders nor resolves dependencies.
    """
    seen: set[str] = set()
    later = {plugin.name for plugin in plugins}
    for plugin in plugins:
        for requirement in sorted(plugin.requires):
            if requirement in seen:
                continue
            if requirement in later:
                raise EasyMigrateError(
                    f"plugin {plugin.name!r} requires {requirement!r}, which runs later; "
                    f"move {requirement!r} before it in registry.PLUGINS"
                )
            raise EasyMigrateError(
                f"plugin {plugin.name!r} requires {requirement!r}, which is not registered"
            )
        seen.add(plugin.name)


def analyze(
    project: Project, *, plugins: Sequence[Plugin] | None = None, strict: bool = False
) -> IRDocument:
    plugins = registry.PLUGINS if plugins is None else plugins
    check_plugin_order(plugins)
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
                "current_params": decision.params,
            }
            strategy_id, acknowledged = choose(view)
            decided_by = decision.decided_by if strategy_id == decision.strategy else "user"
            params = (
                decision.params
                if strategy_id == decision.strategy
                else rule.default_params(diff, strategy_id)
            )
            updated.append(
                Decision(
                    key=decision.key,
                    strategy=strategy_id,
                    decided_by=decided_by,
                    acknowledged=acknowledged,
                    params=params,
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
    """Hashes of every file in terraform/, not just the `.tf` sources: `terraform.tfvars`
    carries the GCP project and region and must also invalidate a stale plan (spec §5.4
    step 6: every file in terraform/ is hashed)."""
    if not terraform_dir.is_dir():
        return {}
    return {f.name: _hash_file(f) for f in sorted(terraform_dir.iterdir()) if f.is_file()}


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


def _primary_instances(document: IRDocument) -> list[DatabaseInstance]:
    """Non-excluded primary `database_instance`s: replicas need no password of their
    own (a Cloud SQL replica inherits its primary's users), and an excluded instance
    was never going to be generated."""
    return [
        instance
        for instance in document.of_kind(DatabaseInstance)
        if instance.excluded is None and instance.role == "primary"
    ]


def _db_password_env_var(ir_id: str) -> str:
    """`EASYMIGRATE_DB_PASSWORD_<SLUG>`, where `<SLUG>` is the id after
    `database_instance:`, uppercased with every non-alphanumeric character replaced by
    `_` (spec plan 4, Passwords)."""
    slug = ir_id.split(":", 1)[1]
    return _DB_PASSWORD_ENV_PREFIX + re.sub(r"[^A-Za-z0-9]", "_", slug).upper()


def _validate_db_password(ir_id: str, value: str) -> None:
    if not (1 <= len(value) <= _DB_PASSWORD_MAX_LEN):
        raise EasyMigrateError(
            f"{ir_id}: password must be between 1 and {_DB_PASSWORD_MAX_LEN} characters"
        )
    if value.strip() != value:
        raise EasyMigrateError(f"{ir_id}: password must not have leading or trailing whitespace")
    if "\x00" in value:
        raise EasyMigrateError(f"{ir_id}: password must not contain a NUL character")
    if "\n" in value or "\r" in value:
        raise EasyMigrateError(f"{ir_id}: password must not contain a newline")


def _resolve_db_passwords(
    document: IRDocument,
    db_passwords: dict[str, str] | None,
    ask_password: Callable[[str], str] | None,
) -> dict[str, str]:
    """Resolve one password per primary, non-excluded `database_instance`: an explicit
    `db_passwords[ir_id]` value, then its env var, then `ask_password(ir_id)` when the
    CLI supplied one. Raises, naming every instance still missing a password and both
    non-interactive ways to supply it, when any remain unresolved."""
    db_passwords = db_passwords or {}
    resolved: dict[str, str] = {}
    missing: list[DatabaseInstance] = []
    for instance in _primary_instances(document):
        value = db_passwords.get(instance.id)
        if value is None:
            value = os.environ.get(_db_password_env_var(instance.id))
        if value is None and ask_password is not None:
            value = ask_password(instance.id)
        if value is None:
            missing.append(instance)
            continue
        _validate_db_password(instance.id, value)
        resolved[instance.id] = value

    if missing:
        lines = [f"missing a database password for {len(missing)} instance(s):"]
        for instance in missing:
            lines.append(
                f"  {instance.id}: use --db-password {instance.id}=<value>, or set "
                f"{_db_password_env_var(instance.id)}"
            )
        raise EasyMigrateError("\n".join(lines))
    return resolved


def _required_gcp_services(document: IRDocument) -> set[str]:
    """`sqladmin.googleapis.com` when any Cloud SQL instance will be generated, plus
    `servicenetworking.googleapis.com` when any of them uses Private Services Access
    (spec §6.4)."""
    instances = [
        instance for instance in document.of_kind(DatabaseInstance) if instance.excluded is None
    ]
    if not instances:
        return set()
    services = {"sqladmin.googleapis.com"}
    if any(instance.target_hints.get("psa") is not None for instance in instances):
        services.add("servicenetworking.googleapis.com")
    return services


def plan(
    project: Project,
    *,
    gcp_project: str,
    gcp_region: str | None = None,
    confirm_bucket: Callable[[str], bool],
    db_passwords: dict[str, str] | None = None,
    ask_password: Callable[[str], str] | None = None,
    tofu: Any = None,
    gcp: Any = None,
    stream: Callable[[str], None] | None = None,
) -> PlanSummary:
    """Generate GCP Terraform from the resolved IR and run `tofu plan` (spec §5.4).

    `stream`, when given, receives progress and every `tofu plan` output line as it happens.
    """
    say = stream or (lambda _line: None)
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

    gen_findings: list[Finding] = []

    # API preflight (spec §6.4): a missing API is a hard error naming the exact
    # `gcloud` command; a failure to *check* (e.g. no permission to read Service
    # Usage) is only a warning, since it must not block a user whose APIs are already
    # enabled.
    required_services = _required_gcp_services(resolved.document)
    if required_services:
        missing_services = gcp.services_enabled(gcp_project, required_services)
        if missing_services is None:
            gen_findings.append(
                Finding(
                    level="info",
                    code="GCP-SERVICES-CHECK-FAILED",
                    message=(
                        "could not verify which GCP APIs are enabled (no permission to read "
                        "Service Usage); continuing without checking "
                        f"{', '.join(sorted(required_services))}"
                    ),
                )
            )
        elif missing_services:
            raise EasyMigrateError(
                "required GCP API(s) not enabled on "
                f"{gcp_project}: {', '.join(sorted(missing_services))}\n"
                f"Run: gcloud services enable {' '.join(sorted(missing_services))} "
                f"--project {gcp_project}"
            )

    # Passwords (spec §2.2, plan 4): resolved before anything touches GCP or OpenTofu,
    # so a missing/invalid password fails fast without creating a state bucket.
    resolved_passwords = _resolve_db_passwords(resolved.document, db_passwords, ask_password)

    # Local preflight (spec §5.4): resolve OpenTofu before touching GCP, so a run that
    # can't get OpenTofu (e.g. no network) never leaves a real state bucket behind. There
    # is no proactive check here for the `compute.googleapis.com` API being enabled on the
    # project; that's handled reactively via the stderr hint in engine/tofu.py instead.
    terraform_dir = project.terraform_dir
    tofu_obj = tofu
    if tofu_obj is None:
        binary = registry.ensure_tofu()
        tofu_obj = registry.new_tofu(binary, terraform_dir)

    bucket = f"{gcp_project}{_STATE_BUCKET_SUFFIX}"
    say(f"Checking the state bucket gs://{bucket}")
    ok = gcp.ensure_bucket(gcp_project, bucket, region, confirm_bucket)
    if not ok:
        raise Declined(
            f"the state bucket gs://{bucket} does not exist and creating it was not approved. "
            "EasyMigrate keeps Terraform state there, so plan cannot continue without it: "
            "tick 'Create the Terraform state bucket' in the UI, or re-run with "
            "--create-state-bucket (or answer y at the prompt)."
        )

    if terraform_dir.is_dir():
        for old in terraform_dir.glob("*.tf"):
            old.unlink()
    terraform_dir.mkdir(parents=True, exist_ok=True)

    password_variables = registry.database_password_variables(resolved.document)
    if password_variables:
        project.write_secrets(
            {password_variables[ir_id]: value for ir_id, value in resolved_passwords.items()}
        )

    decided_by = {decision.key: decision.decided_by for decision in decisions.decisions}
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

    say("Running tofu init")
    tofu_obj.init(backend_config={"bucket": bucket, "prefix": "easymigrate"})
    say("Running tofu plan")
    plan_text = (
        tofu_obj.plan(out=project.tfplan_path, stream=stream)
        if stream is not None
        else tofu_obj.plan(out=project.tfplan_path)
    )
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
