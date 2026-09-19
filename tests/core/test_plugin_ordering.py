"""Declared plugin ordering (spec 4 §2.2).

With three plugins and compute reading network objects out of `ctx.objects`, an accidental
reordering used to surface as a KeyError inside a normalizer. `analyze` now validates the
sequence before running any plugin.
"""

from __future__ import annotations

from typing import Any

import pytest

from easymigrate.core import registry
from easymigrate.core.errors import EasyMigrateError
from easymigrate.core.pipeline import check_plugin_order
from easymigrate.core.plugin import CollectContext, NormalizeContext, Snapshot
from easymigrate.ir.base import IntentObject


class StubPlugin:
    def __init__(self, name: str, requires: frozenset[str] = frozenset()) -> None:
        self.name = name
        self.requires = requires
        self.aws_resource_types: frozenset[str] = frozenset()
        self.normalized = False

    def collect_aws(self, connection: Any, ctx: CollectContext) -> dict[str, Any]:
        return {}

    def normalize(self, snapshot: Snapshot, ctx: NormalizeContext) -> list[IntentObject]:
        self.normalized = True
        return []

    def generate_gcp(self, resolved: Any, ctx: Any) -> Any:
        raise NotImplementedError


def test_registry_order_satisfies_every_requirement():
    check_plugin_order(registry.PLUGINS)


def test_every_registered_requirement_names_a_registered_plugin():
    names = {plugin.name for plugin in registry.PLUGINS}
    for plugin in registry.PLUGINS:
        assert plugin.requires <= names, f"{plugin.name} requires an unregistered plugin"


def test_requirement_later_in_the_sequence_is_rejected():
    network = StubPlugin("network")
    compute = StubPlugin("compute", frozenset({"network"}))
    with pytest.raises(EasyMigrateError) as exc:
        check_plugin_order((compute, network))
    message = str(exc.value)
    assert "compute" in message
    assert "network" in message
    assert "later" in message


def test_absent_requirement_is_rejected_and_distinguished_from_late():
    compute = StubPlugin("compute", frozenset({"network"}))
    with pytest.raises(EasyMigrateError) as exc:
        check_plugin_order((compute,))
    message = str(exc.value)
    assert "compute" in message
    assert "network" in message
    assert "not registered" in message
    assert "later" not in message


def test_a_valid_order_passes():
    network = StubPlugin("network")
    compute = StubPlugin("compute", frozenset({"network"}))
    check_plugin_order((network, compute))


def test_analyze_validates_before_normalizing_anything(tmp_path, monkeypatch):
    """The whole point of the check: fail loudly, not as a KeyError mid-normalize."""
    from easymigrate.core import pipeline
    from easymigrate.core.project import Project

    project = Project(tmp_path)
    project.init()
    snapshot_dir = project.snapshot_dir("aws")
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    project.write_json(
        snapshot_dir / "_scan.json",
        {
            "account_id": "1",
            "region": "ap-south-1",
            "scanned_at": "2026-09-19T00:00:00+00:00",
            "findings": [],
        },
    )

    network = StubPlugin("network")
    compute = StubPlugin("compute", frozenset({"network"}))

    with pytest.raises(EasyMigrateError):
        pipeline.analyze(project, plugins=(compute, network))

    assert compute.normalized is False
    assert network.normalized is False
