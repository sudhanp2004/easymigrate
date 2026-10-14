"""Saved-state schema versioning (ir.versioning): the guard and the upgrade rules."""

from __future__ import annotations

import hashlib
import importlib
import json
import pkgutil

import pytest

import easymigrate.ir
from easymigrate.differences.model import DecisionSet, Difference
from easymigrate.ir import versioning
from easymigrate.ir.base import _KINDS, Finding, IRDocument
from easymigrate.ir.versioning import SCHEMA_VERSION, NewerSchema, StaleSchema, upgrade

# Fingerprint of every model a saved file is read into, per schema version. When this test
# fails you changed what EasyMigrate writes to .easymigrate/: bump SCHEMA_VERSION, add a
# MIGRATIONS step for the old version if the change is mechanical (a new field with a default
# is `lambda data: data`), and record the new fingerprint here.
FINGERPRINTS = {
    "1": "d53bdd0ed5398f9b151477e379b59893301a577730340ddafd443f3bc12bd4f1",
}


def _fingerprint() -> str:
    for module in pkgutil.iter_modules(easymigrate.ir.__path__):
        importlib.import_module(f"easymigrate.ir.{module.name}")
    # Tests register throwaway kinds of their own; only the shipped ones are saved state.
    models = {
        f"kind:{kind}": cls
        for kind, cls in _KINDS.items()
        if cls.__module__.startswith("easymigrate.ir.")
    }
    models |= {
        "IRDocument": IRDocument,
        "Finding": Finding,
        "Difference": Difference,
        "DecisionSet": DecisionSet,
    }
    schemas = {name: models[name].model_json_schema() for name in sorted(models)}
    return hashlib.sha256(json.dumps(schemas, sort_keys=True).encode()).hexdigest()


def test_saved_state_models_match_the_schema_version():
    actual = _fingerprint()
    assert FINGERPRINTS.get(SCHEMA_VERSION) == actual, (
        f"The saved-state models changed (fingerprint {actual}). Bump SCHEMA_VERSION in "
        "easymigrate/ir/versioning.py, add a MIGRATIONS step if old files can be carried "
        "forward, and record the fingerprint in FINGERPRINTS."
    )


def test_current_and_unversioned_files_load_unchanged():
    assert upgrade({"schema_version": SCHEMA_VERSION, "a": 1}, "ir.json", "analyze") == {
        "schema_version": SCHEMA_VERSION,
        "a": 1,
    }
    assert upgrade({"a": 1}, "ir.json", "analyze")["schema_version"] == SCHEMA_VERSION


def test_a_newer_file_is_refused_with_upgrade_advice():
    with pytest.raises(NewerSchema, match="Upgrade EasyMigrate"):
        upgrade({"schema_version": str(int(SCHEMA_VERSION) + 1)}, "ir.json", "analyze")


def test_an_older_file_without_a_migration_says_which_command_rebuilds_it(monkeypatch):
    monkeypatch.setattr(versioning, "SCHEMA_VERSION", "3")
    monkeypatch.setattr(versioning, "MIGRATIONS", {"2": lambda d: d})
    with pytest.raises(StaleSchema, match="run `easymigrate resolve` again"):
        upgrade({"schema_version": "1"}, "decisions.yaml", "resolve")


def test_migrations_run_step_by_step(monkeypatch):
    monkeypatch.setattr(versioning, "SCHEMA_VERSION", "3")
    monkeypatch.setattr(
        versioning,
        "MIGRATIONS",
        {"1": lambda d: d | {"one": True}, "2": lambda d: d | {"two": d["one"]}},
    )
    assert upgrade({"schema_version": "1"}, "ir.json", "analyze") == {
        "schema_version": "3",
        "one": True,
        "two": True,
    }


def test_project_refuses_a_newer_ir_and_cli_prints_it(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from easymigrate.cli import app
    from easymigrate.core.project import Project

    project = Project(tmp_path)
    project.init()
    project.ir_path.write_text(json.dumps({"schema_version": "99", "source": {}, "objects": []}))
    with pytest.raises(NewerSchema):
        project.read_ir(project.ir_path, produced_by="analyze")

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["resolve", "--accept-defaults"])
    assert result.exit_code == 1
    assert "newer version of EasyMigrate" in result.output


def test_decisions_yaml_goes_through_the_same_gate():
    from easymigrate.differences.decisions import from_yaml

    with pytest.raises(NewerSchema):
        from_yaml("schema_version: '99'\ndifferences_fingerprint: x\ndecisions: []\n")
