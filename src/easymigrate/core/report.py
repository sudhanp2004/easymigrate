"""The migration report: terminal (Rich), markdown and json views (spec §6.1, plan 4 B1).

`build_report` reads whichever of ir.json, differences.json, decisions.yaml and
plan_summary.json exist on disk and assembles a `Report`. It is called after
`analyze`, `resolve` and `plan`, so earlier stages leave later sections empty rather
than failing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from easymigrate.core.project import Project
from easymigrate.differences.decisions import from_yaml
from easymigrate.differences.engine import catalogue
from easymigrate.differences.model import DifferenceRule
from easymigrate.ir.base import Finding, IRDocument

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
class PlanSummaryView:
    add: int
    change: int
    destroy: int
    changed_or_destroyed: list[str]
    plan_text_path: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "add": self.add,
            "change": self.change,
            "destroy": self.destroy,
            "changed_or_destroyed": self.changed_or_destroyed,
            "plan_text_path": self.plan_text_path,
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
    )


def _load_superseded(project: Project) -> list[dict[str, Any]]:
    if not project.superseded_path.exists():
        return []
    return json.loads(project.superseded_path.read_text(encoding="utf-8"))


def build_report(project: Project) -> Report:
    config = project.load_config() if project.config_path.exists() else None
    source = config.source.cloud if config and config.source else None
    target = config.target.cloud if config and config.target else None

    ir = _load_ir(project)
    decisions = _load_decisions(project)
    plan_summary = _load_plan_summary(project)
    superseded = _load_superseded(project)

    rules_by_id: dict[str, DifferenceRule] = {rule.id: rule for rule in catalogue()}

    scan_gaps: list[Finding] = []
    not_supported: list[Finding] = []
    info: list[Finding] = []
    if ir is not None:
        for finding in ir.findings:
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
            risk = strategy.risk if strategy else "none"
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
