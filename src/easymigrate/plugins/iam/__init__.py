"""IAM plugin: roles and policies, reported only (spec 8)."""

from typing import Any

from easymigrate.core.plugin import CollectContext, GenerateContext, NormalizeContext, Snapshot
from easymigrate.ir.base import IntentObject, IRDocument
from easymigrate.plugins.iam.collect_aws import collect
from easymigrate.plugins.iam.generate_gcp import generate
from easymigrate.plugins.iam.normalize import normalize
from easymigrate.providers.gcp.hcl import HclFile


class IamPlugin:
    name = "iam"
    requires: frozenset[str] = frozenset()
    aws_resource_types = frozenset(
        {
            "iam:role",
            "iam:user",
            "iam:group",
            "iam:policy",
            "iam:instance-profile",
            "iam:oidc-provider",
        }
    )

    def collect_aws(self, connection: Any, ctx: CollectContext) -> dict[str, Any]:
        return collect(connection, ctx)

    def normalize(self, snapshot: Snapshot, ctx: NormalizeContext) -> list[IntentObject]:
        return normalize(snapshot, ctx)

    def generate_gcp(self, resolved: IRDocument, ctx: GenerateContext) -> HclFile:
        return generate(resolved, ctx)
