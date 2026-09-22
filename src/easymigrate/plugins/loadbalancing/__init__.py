"""Load-balancing plugin: ALBs and NLBs with instance targets (spec 7)."""

from typing import Any

from easymigrate.core.plugin import CollectContext, GenerateContext, NormalizeContext, Snapshot
from easymigrate.ir.base import IntentObject, IRDocument
from easymigrate.plugins.loadbalancing.collect_aws import collect
from easymigrate.plugins.loadbalancing.generate_gcp import generate
from easymigrate.plugins.loadbalancing.normalize import normalize
from easymigrate.providers.gcp.hcl import HclFile


class LoadBalancingPlugin:
    name = "loadbalancing"
    # Targets resolve to compute objects, and load balancers to subnets and security groups.
    requires = frozenset({"network", "compute"})
    aws_resource_types = frozenset(
        {"elasticloadbalancing:loadbalancer", "elasticloadbalancing:targetgroup"}
    )

    def collect_aws(self, connection: Any, ctx: CollectContext) -> dict[str, Any]:
        return collect(connection, ctx)

    def normalize(self, snapshot: Snapshot, ctx: NormalizeContext) -> list[IntentObject]:
        return normalize(snapshot, ctx)

    def generate_gcp(self, resolved: IRDocument, ctx: GenerateContext) -> HclFile:
        return generate(resolved, ctx)
