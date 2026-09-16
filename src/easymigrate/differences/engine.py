"""Detects differences and resolves an IR document against user decisions (spec §4.2)."""

from __future__ import annotations

from easymigrate.differences.model import (
    DecisionSet,
    Difference,
    DifferenceRule,
    DifferenceRuleError,
)
from easymigrate.differences.rules.aws_gcp import AWS_GCP_RULES
from easymigrate.ir.base import IRDocument, ResolutionRef, StrictModel


class SupersededDecision(StrictModel):
    """A decision whose primary subject was removed or excluded by an earlier
    difference's strategy, so its own strategy was never applied (spec §4.2)."""

    key: str
    strategy: str
    subject: str
    superseded_by: str
    message: str


class ResolvedIR(StrictModel):
    document: IRDocument
    superseded: list[SupersededDecision]


def catalogue() -> tuple[DifferenceRule, ...]:
    return AWS_GCP_RULES


def detect(ir: IRDocument, rules: tuple[DifferenceRule, ...] | None = None) -> list[Difference]:
    rules = catalogue() if rules is None else rules
    differences: list[Difference] = []
    seen: set[str] = set()
    for rule in rules:
        try:
            found = rule.detect(ir)
        except Exception as exc:
            raise DifferenceRuleError(rule.id, exc) from exc
        for diff in found:
            if diff.key in seen:
                raise ValueError(f"duplicate difference key: {diff.key}")
            seen.add(diff.key)
            differences.append(diff)
    return sorted(differences, key=lambda d: d.key)


def _superseded_message(
    diff: Difference,
    decision_strategy: str,
    subject: str,
    culprit_key: str,
    culprit_strategy: str,
    *,
    removed: bool,
) -> str:
    verb = "was merged away by" if removed else "was excluded by"
    return (
        f"{diff.key} ({decision_strategy}) no longer applies: "
        f"{subject} {verb} {culprit_key} ({culprit_strategy})."
    )


def resolve_ir(
    ir: IRDocument,
    differences: list[Difference],
    decisions: DecisionSet,
    rules: tuple[DifferenceRule, ...] | None = None,
) -> ResolvedIR:
    rules = catalogue() if rules is None else rules
    by_rule: dict[str, list[Difference]] = {}
    for diff in differences:
        by_rule.setdefault(diff.rule_id, []).append(diff)
    decisions_by_key = {decision.key: decision for decision in decisions.decisions}

    doc = ir
    superseded: list[SupersededDecision] = []
    # Object id -> key of the difference whose apply made that object disappear or
    # become excluded. The engine, not the rules, is the source of truth for this.
    culprit_by_object: dict[str, str] = {}

    for rule in rules:
        for diff in sorted(by_rule.get(rule.id, []), key=lambda d: d.key):
            decision = decisions_by_key.get(diff.key)
            if decision is None:
                raise ValueError(f"missing decision for difference {diff.key}")

            subject = diff.subjects[0]
            current = next((o for o in doc.objects if o.id == subject), None)
            if current is None or current.excluded is not None:
                culprit_key = culprit_by_object.get(subject, diff.key)
                culprit_decision = decisions_by_key.get(culprit_key)
                culprit_strategy = culprit_decision.strategy if culprit_decision else "?"
                message = _superseded_message(
                    diff,
                    decision.strategy,
                    subject,
                    culprit_key,
                    culprit_strategy,
                    removed=current is None,
                )
                superseded.append(
                    SupersededDecision(
                        key=diff.key,
                        strategy=decision.strategy,
                        subject=subject,
                        superseded_by=culprit_key,
                        message=message,
                    )
                )
                continue

            before = {obj.id: obj.excluded for obj in doc.objects}
            try:
                doc = rule.apply(doc, diff, decision.strategy, decision.params)
            except Exception as exc:
                raise DifferenceRuleError(rule.id, exc) from exc
            ref = ResolutionRef(key=diff.key, strategy=decision.strategy)
            objects = [
                obj.model_copy(update={"resolution": [*obj.resolution, ref]})
                if obj.id in diff.subjects
                else obj
                for obj in doc.objects
            ]
            doc = doc.model_copy(update={"objects": objects})
            doc = IRDocument.model_validate_json(doc.model_dump_json())

            after = {obj.id: obj.excluded for obj in doc.objects}
            for object_id, excluded_before in before.items():
                if excluded_before is not None:
                    continue
                if object_id not in after or after[object_id] is not None:
                    culprit_by_object[object_id] = diff.key

    return ResolvedIR(document=doc, superseded=superseded)
