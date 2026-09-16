from easymigrate.core.plugin import GenerateContext, Plugin
from easymigrate.core.registry import PLUGINS
from easymigrate.ir.base import IRDocument
from easymigrate.plugins.database import DatabasePlugin
from easymigrate.providers.gcp.hcl import HclFile


def test_plugin_satisfies_protocol_and_declares_resource_types():
    plugin: Plugin = DatabasePlugin()

    assert plugin.name == "database"
    assert "rds:db" in plugin.aws_resource_types
    assert "rds:cluster" in plugin.aws_resource_types
    assert "rds:subgrp" in plugin.aws_resource_types
    assert "rds:pg" in plugin.aws_resource_types
    assert "ec2:vpc" not in plugin.aws_resource_types


def test_generate_gcp_delegates_to_the_generator():
    resolved = IRDocument(source={"cloud": "aws"}, objects=[])
    ctx = GenerateContext(project="p", region="asia-south1")

    result = DatabasePlugin().generate_gcp(resolved, ctx)

    assert isinstance(result, HclFile)
    assert result.blocks == []


def test_registry_orders_network_before_database():
    assert [p.name for p in PLUGINS] == ["network", "database"]
