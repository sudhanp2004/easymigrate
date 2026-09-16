"""No silent mapping (plan 3 A1/A2, D19): every IR field this generator could touch
must be declared in FIELD_COVERAGE, so adding a field without declaring it fails CI."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from easymigrate.differences.engine import catalogue
from easymigrate.ir.database import (
    DatabaseInstance,
    DbBackup,
    DbCompute,
    DbMaintenance,
    DbNetwork,
    DbStorage,
)
from easymigrate.plugins.database.generate_gcp import FIELD_COVERAGE

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT / "scripts"))

from gen_differences_doc import field_coverage_output_path, render_field_coverage  # noqa: E402

_KINDS = {
    "DatabaseInstance": DatabaseInstance,
    "DbCompute": DbCompute,
    "DbStorage": DbStorage,
    "DbNetwork": DbNetwork,
    "DbBackup": DbBackup,
    "DbMaintenance": DbMaintenance,
}

_RULES_SRC = Path(__file__).resolve().parents[3] / "src" / "easymigrate" / "differences" / "rules"


@pytest.mark.parametrize("kind_name, model", sorted(_KINDS.items()))
def test_every_field_is_declared(kind_name, model):
    declared = set(FIELD_COVERAGE[kind_name])
    assert declared == set(model.model_fields)


def test_every_model_has_coverage():
    assert set(FIELD_COVERAGE) == set(_KINDS)


def _all_declarations():
    for kind, fields in FIELD_COVERAGE.items():
        for field, declaration in fields.items():
            yield kind, field, declaration


@pytest.mark.parametrize("kind, field, declaration", list(_all_declarations()))
def test_declarations_use_an_allowed_value(kind, field, declaration):
    assert (
        declaration == "exact"
        or declaration == "metadata"
        or declaration.startswith(("rule:", "hint:", "not_generated:", "derived:", "metadata:"))
    )


def test_every_rule_id_exists_in_catalogue():
    known = {rule.id for rule in catalogue()}
    for _kind, _field, declaration in _all_declarations():
        if declaration.startswith("rule:"):
            rule_id = declaration.removeprefix("rule:")
            assert rule_id in known, f"unknown rule id {rule_id!r} in FIELD_COVERAGE"


def test_every_hint_key_is_produced_by_a_rule():
    source = "\n".join(p.read_text() for p in _RULES_SRC.rglob("*.py"))
    for _kind, _field, declaration in _all_declarations():
        if declaration.startswith("hint:"):
            key = declaration.removeprefix("hint:")
            pattern = re.compile(rf'["\']{re.escape(key)}["\']')
            assert pattern.search(source), f"hint key {key!r} is not produced by any rule"


def test_field_coverage_doc_is_up_to_date():
    expected = render_field_coverage("database")
    actual = field_coverage_output_path("database").read_text(encoding="utf-8")
    assert actual == expected, (
        "docs/field-coverage-database.md is stale; run "
        "`uv run python scripts/gen_differences_doc.py` and commit the result"
    )
