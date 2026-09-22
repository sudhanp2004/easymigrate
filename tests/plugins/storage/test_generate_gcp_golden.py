"""Storage golden tests (spec 6 plan 3): snapshot -> normalize -> detect -> resolve with defaults
-> generate for every fixture, compared with expected_storage.tf. Regenerate with
`uv run pytest --update-golden tests/plugins/storage/` and review the diff."""

from __future__ import annotations

from pathlib import Path

import pytest

from easymigrate.core.plugin import GenerateContext, NormalizeContext, Snapshot
from easymigrate.differences.decisions import merge_decisions
from easymigrate.differences.engine import catalogue, detect, resolve_ir
from easymigrate.ir.base import IRDocument
from easymigrate.plugins.storage.generate_gcp import generate
from easymigrate.plugins.storage.normalize import normalize

FIXTURES = Path(__file__).parent / "fixtures"
CASES = sorted(p.name for p in FIXTURES.glob("*") if (p / "snapshot").is_dir())


def resolved_case(case: str):
    ctx = NormalizeContext(account_id="123456789012", region="ap-south-1", strict=True)
    objects = normalize(Snapshot.from_dir(FIXTURES / case / "snapshot"), ctx)
    ir_doc = IRDocument(
        source={"cloud": "aws", "account_id": "123456789012", "region": "ap-south-1"},
        objects=objects,
        findings=ctx.findings,
    )
    rules = catalogue()
    differences = detect(ir_doc, rules)
    merged, _ = merge_decisions(None, differences, rules)
    by_id = {rule.id: rule for rule in rules}
    by_key = {diff.key: diff for diff in differences}
    decisions = [
        d.model_copy(update={"acknowledged": True})
        if by_id[by_key[d.key].rule_id].strategy(d.strategy).manual
        else d
        for d in merged.decisions
    ]
    merged = merged.model_copy(update={"decisions": decisions})
    resolved = resolve_ir(ir_doc, differences, merged, rules)
    ctx = GenerateContext(project="test-project", region="asia-south1", version="test")
    ctx.decided_by.update({d.key: d.decided_by for d in merged.decisions})
    return resolved.document, ctx


def test_every_case_has_a_fixture():
    assert CASES


@pytest.mark.parametrize("case", CASES)
def test_generate_golden(case, update_golden):
    document, ctx = resolved_case(case)
    actual = generate(document, ctx).render()
    expected = FIXTURES / case / "expected_storage.tf"
    if update_golden:
        expected.write_text(actual)
    assert expected.exists(), "missing golden file; run pytest --update-golden"
    assert actual == expected.read_text()
