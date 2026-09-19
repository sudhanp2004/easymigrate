"""Generator golden tests (spec 4 plan 3 task 5): the real scan->IR->generate path -
Snapshot.from_dir -> network normalize -> compute normalize -> detect -> resolve_ir
with defaults -> generate - for every fixture case under
tests/plugins/compute/fixtures/, compared against a hand-written expected_compute.tf.

Regenerate the expected files with:
    uv run pytest --update-golden tests/plugins/compute/test_generate_gcp_golden.py
and review the diff before committing -- the golden files are written by hand from
the plan and spec, not recorded from generator output, so a `--update-golden` diff
that looks wrong means the generator is wrong, not the golden."""

from __future__ import annotations

from pathlib import Path

import pytest

from easymigrate.core.plugin import GenerateContext, NormalizeContext, Snapshot
from easymigrate.differences.decisions import merge_decisions
from easymigrate.differences.engine import catalogue, detect, resolve_ir
from easymigrate.differences.model import Decision
from easymigrate.ir.base import IRDocument
from easymigrate.plugins.compute.generate_gcp import generate
from easymigrate.plugins.compute.normalize import normalize as normalize_compute
from easymigrate.plugins.network.normalize import normalize as normalize_network

FIXTURES = Path(__file__).parent / "fixtures"
CASES = sorted(p.name for p in FIXTURES.glob("*") if (p / "snapshot").is_dir())


def _normalize(case: str) -> IRDocument:
    ctx = NormalizeContext(account_id="123456789012", region="ap-south-1", strict=True)
    snapshot = Snapshot.from_dir(FIXTURES / case / "snapshot")
    network_objects = normalize_network(snapshot, ctx)
    ctx.objects.update({obj.id: obj for obj in network_objects})
    compute_objects = normalize_compute(snapshot, ctx)
    return IRDocument(
        source={"cloud": "aws", "account_id": "123456789012", "region": "ap-south-1"},
        objects=[*network_objects, *compute_objects],
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

    expected_path = FIXTURES / case / "expected_compute.tf"
    if update_golden:
        expected_path.write_text(actual)

    assert expected_path.exists(), "missing golden file; run pytest --update-golden"
    assert actual == expected_path.read_text()
