"""Detects differences and resolves an IR document against user decisions (spec §4.2)."""

from __future__ import annotations

from easymigrate.differences.model import (
    DecisionSet,
    Difference,
    DifferenceRule,
    DifferenceRuleError,
)
from easymigrate.differences.rules.aws_gcp import AWS_GCP_RULES
from easymigrate.ir.base import IRDocument, ResolutionRef


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


def resolve_ir(
    ir: IRDocument,
    differences: list[Difference],
    decisions: DecisionSet,
    rules: tuple[DifferenceRule, ...] | None = None,
) -> IRDocument:
    rules = catalogue() if rules is None else rules
    by_rule: dict[str, list[Difference]] = {}
    for diff in differences:
        by_rule.setdefault(diff.rule_id, []).append(diff)
    decisions_by_key = {decision.key: decision for decision in decisions.decisions}

    doc = ir
    for rule in rules:
        for diff in sorted(by_rule.get(rule.id, []), key=lambda d: d.key):
            decision = decisions_by_key.get(diff.key)
            if decision is None:
                raise ValueError(f"missing decision for difference {diff.key}")
            try:
                doc = rule.apply(doc, diff, decision.strategy)
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
    return doc
