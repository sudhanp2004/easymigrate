"""Generator golden tests (plan 4 A5): normalize -> detect -> merge_decisions (all
defaults, every manual decision acknowledged) -> resolve_ir -> generate, for every
fixture case under tests/plugins/network/fixtures/.

Regenerate the expected files with:
    uv run pytest --update-golden tests/plugins/network/test_generate_gcp_golden.py
and review the diff before committing."""

from __future__ import annotations

from pathlib import Path

import pytest

from easymigrate.core.plugin import GenerateContext, NormalizeContext, Snapshot
from easymigrate.differences.decisions import merge_decisions
from easymigrate.differences.engine import catalogue, detect, resolve_ir
from easymigrate.differences.model import Decision
from easymigrate.ir.base import IRDocument
from easymigrate.plugins.network import NetworkPlugin
from easymigrate.plugins.network.generate_gcp import generate

FIXTURES = Path(__file__).parent / "fixtures"
CASES = sorted(p.name for p in FIXTURES.glob("*") if (p / "snapshot").is_dir())


def _normalize(case: str) -> IRDocument:
    ctx = NormalizeContext(account_id="123456789012", region="ap-south-1", strict=True)
    objects = NetworkPlugin().normalize(Snapshot.from_dir(FIXTURES / case / "snapshot"), ctx)
    return IRDocument(
        source={"cloud": "aws", "account_id": "123456789012", "region": "ap-south-1"},
        objects=objects,
        findings=ctx.findings,
    )


def _resolve_with_defaults(ir_doc: IRDocument):
    rules = catalogue()
    differences = detect(ir_doc, rules)
    merged, _dropped = merge_decisions(None, differences, rules)

    rules_by_id = {rule.id: rule for rule in rules}
    diffs_by_key = {diff.key: diff for diff in differences}
    acked: list[Decision] = []
    for decision in merged.decisions:
        diff = diffs_by_key[decision.key]
        rule = rules_by_id[diff.rule_id]
        strategy = rule.strategy(decision.strategy)
        if strategy.manual:
            decision = decision.model_copy(update={"acknowledged": True})
        acked.append(decision)
    merged = merged.model_copy(update={"decisions": acked})

    resolved = resolve_ir(ir_doc, differences, merged, rules)
    decided_by = {decision.key: decision.decided_by for decision in merged.decisions}
    return resolved, decided_by


def test_every_case_has_a_fixture():
    assert CASES, "no fixture cases found"


@pytest.mark.parametrize("case", CASES)
def test_generate_golden(case, update_golden):
    ir_doc = _normalize(case)
    resolved, decided_by = _resolve_with_defaults(ir_doc)

    ctx = GenerateContext(project="test-project", region="asia-south1", version="test")
    ctx.decided_by.update(decided_by)
    actual = generate(resolved.document, ctx).render()

    expected_path = FIXTURES / case / "expected_network.tf"
    if update_golden:
        expected_path.write_text(actual)

    assert expected_path.exists(), "missing golden file; run pytest --update-golden"
    assert actual == expected_path.read_text()
