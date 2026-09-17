"""Response models for the review step, assembled from engine types (spec §5).

Every field here is derived through the rule hooks (`allowed_strategies`,
`default_strategy`, `risk_for`) rather than the class-level `strategies`/`default`/
static `Strategy.risk` — five rules vary their strategy set per difference, and a UI
offering a strategy a difference disallows produces a crash later in `resolve_ir`.
"""

from __future__ import annotations

from typing import Any, Literal

from easymigrate.differences.model import Decision, Difference, DifferenceRule
from easymigrate.ir.base import StrictModel


class StrategyView(StrictModel):
    id: str
    title: str
    effect: str
    risk: Literal["none", "low", "medium", "high"]
    risk_note: str | None
    manual: bool
    is_default: bool


class DifferenceView(StrictModel):
    key: str
    rule_id: str
    family: str
    title: str
    impact: str
    subjects: list[str]
    context: dict[str, Any]
    strategies: list[StrategyView]
    param_docs: dict[str, str]


class DecisionView(StrictModel):
    key: str
    strategy: str
    decided_by: Literal["default", "user"]
    acknowledged: bool
    params: dict[str, Any]
    needs_acknowledgement: bool


def difference_view(diff: Difference, rule: DifferenceRule) -> DifferenceView:
    default_id = rule.default_strategy(diff)
    strategies = [
        StrategyView(
            id=strategy.id,
            title=strategy.title,
            effect=strategy.effect,
            risk=rule.risk_for(diff, strategy.id),
            risk_note=strategy.risk_note,
            manual=strategy.manual,
            is_default=strategy.id == default_id,
        )
        for strategy in rule.allowed_strategies(diff)
    ]
    return DifferenceView(
        key=diff.key,
        rule_id=rule.id,
        family=rule.family,
        title=rule.title,
        impact=rule.impact,
        subjects=diff.subjects,
        context=diff.context,
        strategies=strategies,
        param_docs=dict(rule.param_docs),
    )


def decision_view(decision: Decision, rule: DifferenceRule) -> DecisionView:
    try:
        manual = rule.strategy(decision.strategy).manual
    except KeyError:
        manual = False
    return DecisionView(
        key=decision.key,
        strategy=decision.strategy,
        decided_by=decision.decided_by,
        acknowledged=decision.acknowledged,
        params=decision.params,
        needs_acknowledgement=manual and not decision.acknowledged,
    )
