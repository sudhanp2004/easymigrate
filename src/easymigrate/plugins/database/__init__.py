"""Database plugin: RDS instances and Multi-AZ DB clusters."""

from typing import Any

from easymigrate.core.plugin import CollectContext, GenerateContext, NormalizeContext, Snapshot
from easymigrate.ir.base import IntentObject, IRDocument
from easymigrate.plugins.database.collect_aws import collect
from easymigrate.plugins.database.normalize import normalize
from easymigrate.providers.gcp.hcl import HclFile


class DatabasePlugin:
    name = "database"
    aws_resource_types = frozenset(
        {
            "rds:db",
            "rds:cluster",
            "rds:subgrp",
            "rds:pg",
        }
    )

    def collect_aws(self, connection: Any, ctx: CollectContext) -> dict[str, Any]:
        return collect(connection, ctx)

    def normalize(self, snapshot: Snapshot, ctx: NormalizeContext) -> list[IntentObject]:
        return normalize(snapshot, ctx)

    def generate_gcp(self, resolved: IRDocument, ctx: GenerateContext) -> HclFile:
        return HclFile()
