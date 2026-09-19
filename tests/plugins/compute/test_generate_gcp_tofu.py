"""`tofu validate` test (spec 4 plan 3 task 5), marked `tofu` and excluded from the default
run (see pyproject.toml `addopts`). Needs network access to download OpenTofu and the
google provider the first time; run explicitly with:

    uv run pytest -m tofu

This is the check unit tests cannot do: a wrong attribute name or block nesting on
`google_sql_compute_instance` or `google_service_networking_connection` fails here
even though every HCL-shape assertion in the golden/unit tests would still pass."""

from __future__ import annotations

from pathlib import Path

import pytest

from easymigrate.core.plugin import GenerateContext, NormalizeContext, Snapshot
from easymigrate.differences.decisions import merge_decisions
from easymigrate.differences.engine import catalogue, detect, resolve_ir
from easymigrate.differences.model import Decision
from easymigrate.engine.tofu import Tofu, ensure_tofu
from easymigrate.ir.base import IRDocument
from easymigrate.plugins.compute.generate_gcp import generate as generate_compute
from easymigrate.plugins.compute.normalize import normalize as normalize_compute
from easymigrate.plugins.network.generate_gcp import generate as generate_network
from easymigrate.plugins.network.normalize import normalize as normalize_network
from easymigrate.providers.gcp.boilerplate import render_providers_tf, render_variables_tf

FIXTURES = Path(__file__).parent / "fixtures"

# standalone_linux is the testbed shape (boot disk, key pair, Elastic IP, one security
# group); extra_volumes adds an attached persistent disk and a scratch disk, which is the
# most block nesting this family emits.
CASES = ["standalone_linux", "extra_volumes"]


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


@pytest.mark.tofu
@pytest.mark.parametrize("case", CASES)
def test_compute_tf_validates_alongside_network_tf(case, tmp_path):
    ir_doc = _normalize(case)
    resolved, decided_by = _resolve_with_defaults(ir_doc)

    ctx = GenerateContext(project="test-project", region="asia-south1", version="0.0.0-test")
    ctx.decided_by.update(decided_by)

    network_tf = generate_network(resolved.document, ctx).render()
    compute_tf = generate_compute(resolved.document, ctx).render()

    (tmp_path / "network.tf").write_text(network_tf)
    (tmp_path / "compute.tf").write_text(compute_tf)
    (tmp_path / "variables.tf").write_text(render_variables_tf("0.0.0-test"))
    (tmp_path / "providers.tf").write_text(render_providers_tf("0.0.0-test"))

    binary = ensure_tofu()
    tofu = Tofu(binary, tmp_path)
    tofu.init(backend=False)
    tofu.validate()
