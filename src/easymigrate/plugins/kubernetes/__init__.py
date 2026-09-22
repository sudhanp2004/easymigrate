"""Kubernetes plugin: EKS clusters, managed and self-managed node groups (spec 5)."""

from typing import Any

from easymigrate.core.plugin import CollectContext, GenerateContext, NormalizeContext, Snapshot
from easymigrate.ir.base import IntentObject, IRDocument
from easymigrate.plugins.kubernetes.collect_aws import collect
from easymigrate.plugins.kubernetes.generate_gcp import generate
from easymigrate.plugins.kubernetes.normalize import normalize
from easymigrate.providers.gcp.hcl import HclFile


class KubernetesPlugin:
    name = "kubernetes"
    # Subnets and security groups resolve through `ctx.objects`, which the network plugin fills.
    requires = frozenset({"network"})
    aws_resource_types = frozenset(
        {
            "eks:cluster",
            "eks:nodegroup",
            "eks:fargateprofile",
            "eks:addon",
            "eks:identityproviderconfig",
            "eks:podidentityassociation",
            "eks:access-entry",
        }
    )

    def collect_aws(self, connection: Any, ctx: CollectContext) -> dict[str, Any]:
        return collect(connection, ctx)

    def normalize(self, snapshot: Snapshot, ctx: NormalizeContext) -> list[IntentObject]:
        return normalize(snapshot, ctx)

    def generate_gcp(self, resolved: IRDocument, ctx: GenerateContext) -> HclFile:
        return generate(resolved, ctx)
