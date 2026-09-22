"""Storage plugin: S3 buckets, configuration only (spec 6)."""

from typing import Any

from easymigrate.core.plugin import CollectContext, GenerateContext, NormalizeContext, Snapshot
from easymigrate.ir.base import IntentObject, IRDocument
from easymigrate.plugins.storage.collect_aws import collect
from easymigrate.plugins.storage.generate_gcp import generate
from easymigrate.plugins.storage.normalize import normalize
from easymigrate.providers.gcp.hcl import HclFile


class StoragePlugin:
    name = "storage"
    # Buckets reference no network objects.
    requires: frozenset[str] = frozenset()
    aws_resource_types = frozenset({"s3"})

    def collect_aws(self, connection: Any, ctx: CollectContext) -> dict[str, Any]:
        return collect(connection, ctx)

    def normalize(self, snapshot: Snapshot, ctx: NormalizeContext) -> list[IntentObject]:
        return normalize(snapshot, ctx)

    def generate_gcp(self, resolved: IRDocument, ctx: GenerateContext) -> HclFile:
        return generate(resolved, ctx)
