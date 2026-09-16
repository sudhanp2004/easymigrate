"""Difference engine model: rules, differences and decisions (spec §4.1)."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Any, ClassVar, Literal

from pydantic import Field

from easymigrate.ir.base import IRDocument, StrictModel

Risk = Literal["none", "low", "medium", "high"]

_RULE_ID_RE = re.compile(r"^[A-Z]+(-[A-Z0-9]+)+$")


class Strategy(StrictModel):
    id: str
    title: str
    effect: str
    risk: Risk
    risk_note: str | None = None
    manual: bool = False


class Difference(StrictModel):
    key: str
    rule_id: str
    subjects: list[str]
    context: dict[str, Any]


class DifferenceRuleError(Exception):
    def __init__(self, rule_id: str, cause: BaseException) -> None:
        self.rule_id = rule_id
        self.cause = cause
        super().__init__(f"difference rule {rule_id} failed: {cause}")


class DifferenceRule(ABC):
    id: ClassVar[str]
    family: ClassVar[str]
    title: ClassVar[str]
    impact: ClassVar[str]
    strategies: ClassVar[tuple[Strategy, ...]]
    default: ClassVar[str]
    source: ClassVar[str] = "aws"
    target: ClassVar[str] = "gcp"

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if not _RULE_ID_RE.match(cls.id):
            raise TypeError(f"{cls.__name__}: invalid rule id {cls.id!r}")
        ids = [s.id for s in cls.strategies]
        if len(ids) != len(set(ids)):
            raise TypeError(f"{cls.id}: duplicate strategy ids")
        if cls.default not in ids:
            raise TypeError(f"{cls.id}: default {cls.default!r} is not a declared strategy")

    def strategy(self, strategy_id: str) -> Strategy:
        for candidate in self.strategies:
            if candidate.id == strategy_id:
                return candidate
        raise KeyError(strategy_id)

    def difference(self, subjects: list[str], **context: Any) -> Difference:
        return Difference(
            key=f"{self.id}@{subjects[0]}", rule_id=self.id, subjects=subjects, context=context
        )

    def default_params(self, diff: Difference, strategy_id: str) -> dict[str, Any]:
        return {}

    def validate_params(self, diff: Difference, strategy_id: str, params: dict[str, Any]) -> None:
        return None

    @abstractmethod
    def detect(self, ir: IRDocument) -> list[Difference]: ...

    @abstractmethod
    def apply(
        self,
        ir: IRDocument,
        diff: Difference,
        strategy_id: str,
        params: dict[str, Any] | None = None,
    ) -> IRDocument: ...


class Decision(StrictModel):
    key: str
    strategy: str
    decided_by: Literal["default", "user"]
    acknowledged: bool = False
    params: dict[str, Any] = Field(default_factory=dict)


class DecisionSet(StrictModel):
    schema_version: Literal["1"] = "1"
    differences_fingerprint: str
    decisions: list[Decision]
    acknowledged_in_bulk: bool = False
