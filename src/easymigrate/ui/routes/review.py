"""The review step's HTTP surface: differences, decisions and the report (spec §5,
plan 3). This is the part of the product the UI exists for — reading every difference
with the strategies it actually allows, setting a decision with validated params, and
reading the report.

Reads are stateless (spec's constraint): every request re-loads `differences.json` and
`decisions.yaml` from disk rather than caching engine state in the server process, so
the CLI and UI can be used interchangeably on the same project.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from fastapi import Depends, FastAPI, HTTPException

from easymigrate.core import pipeline
from easymigrate.core.project import Project
from easymigrate.core.report import build_report
from easymigrate.differences.decisions import (
    from_yaml,
    merge_decisions,
    readiness_problems,
    to_yaml,
)
from easymigrate.differences.engine import catalogue
from easymigrate.differences.model import Decision, DifferenceRule
from easymigrate.ir.base import Finding, StrictModel
from easymigrate.ui.routes.deps import RouteDeps
from easymigrate.ui.views import DecisionView, DifferenceView, decision_view, difference_view


class AnalyzeResponse(StrictModel):
    counts: dict[str, int]
    findings: list[Finding]


class DecisionsResponse(StrictModel):
    decisions: list[DecisionView]
    blocked: list[str]


class DecisionWriteRequest(StrictModel):
    strategy: str
    params: dict[str, Any] | None = None
    # spec §5: "optional acknowledgement" — omitted must leave the current value alone;
    # only an explicit `false` may revoke an existing acknowledgement.
    acknowledged: bool | None = None


def _rules_by_id() -> dict[str, DifferenceRule]:
    return {rule.id: rule for rule in catalogue()}


def _decisions_response(project: Project) -> DecisionsResponse:
    differences = pipeline.load_differences(project)
    diffs_by_key = {diff.key: diff for diff in differences}
    rules_by_id = _rules_by_id()
    rules = catalogue()

    existing = (
        from_yaml(project.decisions_path.read_text(encoding="utf-8"))
        if project.decisions_path.exists()
        else None
    )
    blocked = readiness_problems(existing, differences, rules)

    views: list[DecisionView] = []
    if existing is not None:
        for decision in existing.decisions:
            diff = diffs_by_key.get(decision.key)
            if diff is None:
                continue
            views.append(decision_view(decision, rules_by_id[diff.rule_id]))
    return DecisionsResponse(decisions=views, blocked=blocked)


def register(app: FastAPI, deps: RouteDeps) -> None:
    reads = deps.reads
    mutating = deps.mutating
    current = Depends(deps.project)

    @app.post("/api/analyze", dependencies=mutating)
    def analyze(project: Project = current) -> AnalyzeResponse:
        doc = pipeline.analyze(project)
        counts = Counter(obj.kind for obj in doc.objects)
        return AnalyzeResponse(counts=dict(counts), findings=list(doc.findings))

    @app.get("/api/differences", dependencies=reads)
    def get_differences(project: Project = current) -> list[DifferenceView]:
        differences = pipeline.load_differences(project)
        rules_by_id = _rules_by_id()
        views = [difference_view(diff, rules_by_id[diff.rule_id]) for diff in differences]
        return sorted(views, key=lambda view: view.key)

    @app.get("/api/decisions", dependencies=reads)
    def get_decisions(project: Project = current) -> DecisionsResponse:
        return _decisions_response(project)

    @app.put("/api/decisions/{key}", dependencies=mutating)
    def put_decision(
        key: str, body: DecisionWriteRequest, project: Project = current
    ) -> DecisionView:
        differences = pipeline.load_differences(project)
        diffs_by_key = {diff.key: diff for diff in differences}
        if key not in diffs_by_key:
            raise HTTPException(status_code=404, detail=f"no difference with key {key!r}")

        rules = catalogue()
        rules_by_id = _rules_by_id()
        diff = diffs_by_key[key]
        rule = rules_by_id[diff.rule_id]

        allowed = rule.allowed_strategies(diff)
        allowed_ids = {strategy.id for strategy in allowed}
        if body.strategy not in allowed_ids:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"strategy {body.strategy!r} is not allowed for {key}; allowed "
                    f"strategies are: {sorted(allowed_ids)}"
                ),
            )

        existing = (
            from_yaml(project.decisions_path.read_text(encoding="utf-8"))
            if project.decisions_path.exists()
            else None
        )
        merged, _dropped = merge_decisions(existing, differences, rules)
        current = next(d for d in merged.decisions if d.key == key)

        if body.params is not None:
            params = body.params
        elif current.strategy == body.strategy:
            params = current.params
        else:
            params = rule.default_params(diff, body.strategy)

        try:
            rule.validate_params(diff, body.strategy, params)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

        acknowledged = current.acknowledged if body.acknowledged is None else body.acknowledged
        new_decision = Decision(
            key=key,
            strategy=body.strategy,
            decided_by="user",
            acknowledged=acknowledged,
            params=params,
        )
        updated_decisions = [new_decision if d.key == key else d for d in merged.decisions]
        new_set = merged.model_copy(update={"decisions": updated_decisions})
        project.write_text(project.decisions_path, to_yaml(new_set))

        return decision_view(new_decision, rule)

    @app.post("/api/resolve", dependencies=mutating)
    def resolve(project: Project = current) -> DecisionsResponse:
        pipeline.resolve(project, accept_defaults=True)
        return _decisions_response(project)

    @app.get("/api/report", dependencies=reads)
    def get_report(project: Project = current) -> dict[str, Any]:
        return build_report(project).to_dict()
