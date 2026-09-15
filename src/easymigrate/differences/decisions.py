"""Decision persistence: merge, readiness checks and YAML round-trip (spec §4.2)."""

from __future__ import annotations

import hashlib

import yaml

from easymigrate.differences.model import Decision, DecisionSet, Difference, DifferenceRule


def fingerprint(differences: list[Difference]) -> str:
    keys = sorted(diff.key for diff in differences)
    return hashlib.sha256("\n".join(keys).encode()).hexdigest()


def merge_decisions(
    existing: DecisionSet | None,
    differences: list[Difference],
    rules: tuple[DifferenceRule, ...],
) -> tuple[DecisionSet, list[str]]:
    by_id = {rule.id: rule for rule in rules}
    valid_strategies = {
        diff.key: {s.id for s in by_id[diff.rule_id].strategies} for diff in differences
    }

    existing_by_key = (
        {decision.key: decision for decision in existing.decisions} if existing else {}
    )
    dropped: list[str] = [
        key
        for key, decision in existing_by_key.items()
        if key not in valid_strategies or decision.strategy not in valid_strategies[key]
    ]
    dropped_set = set(dropped)

    decisions: list[Decision] = []
    for diff in differences:
        kept = existing_by_key.get(diff.key)
        if kept is not None and diff.key not in dropped_set:
            decisions.append(kept)
        else:
            decisions.append(
                Decision(key=diff.key, strategy=by_id[diff.rule_id].default, decided_by="default")
            )
    decisions.sort(key=lambda d: d.key)

    merged = DecisionSet(differences_fingerprint=fingerprint(differences), decisions=decisions)
    return merged, sorted(dropped)


def readiness_problems(
    decisions: DecisionSet | None,
    differences: list[Difference],
    rules: tuple[DifferenceRule, ...],
) -> list[str]:
    if decisions is None or decisions.differences_fingerprint != fingerprint(differences):
        return ["differences changed since resolve; run `easymigrate resolve`"]

    by_id = {rule.id: rule for rule in rules}
    diffs_by_key = {diff.key: diff for diff in differences}
    problems: list[str] = []
    for decision in decisions.decisions:
        diff = diffs_by_key.get(decision.key)
        if diff is None:
            continue
        rule = by_id[diff.rule_id]
        try:
            strategy = rule.strategy(decision.strategy)
        except KeyError:
            problems.append(f"{decision.key} uses an unknown strategy {decision.strategy!r}")
            continue
        if strategy.manual and not decision.acknowledged:
            problems.append(f"{decision.key} uses a manual strategy and is not acknowledged")
    return problems


def to_yaml(decisions: DecisionSet) -> str:
    return yaml.safe_dump(decisions.model_dump(mode="json"), sort_keys=False)


def from_yaml(text: str) -> DecisionSet:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML: {exc}") from exc
    return DecisionSet.model_validate(data)
