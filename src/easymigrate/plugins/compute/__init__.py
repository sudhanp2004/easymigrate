"""Compute plugin: EC2 instances, EBS volumes, Elastic IPs, launch templates and ASGs."""

from typing import Any

from easymigrate.core.plugin import CollectContext, GenerateContext, NormalizeContext, Snapshot
from easymigrate.ir.base import IntentObject, IRDocument
from easymigrate.plugins.compute.collect_aws import collect
from easymigrate.plugins.compute.generate_gcp import generate
from easymigrate.plugins.compute.normalize import normalize
from easymigrate.providers.gcp.hcl import HclFile


class ComputePlugin:
    name = "compute"
    # The normalizer resolves subnet and security-group native ids through `ctx.objects`,
    # which the network plugin fills (spec 4 §2.2).
    requires = frozenset({"network"})
    aws_resource_types = frozenset(
        {
            "ec2:instance",
            "ec2:volume",
            "ec2:elastic-ip",
            "ec2:launch-template",
            "autoscaling:autoScalingGroup",
        }
    )

    def collect_aws(self, connection: Any, ctx: CollectContext) -> dict[str, Any]:
        return collect(connection, ctx)

    def normalize(self, snapshot: Snapshot, ctx: NormalizeContext) -> list[IntentObject]:
        return normalize(snapshot, ctx)

    def generate_gcp(self, resolved: IRDocument, ctx: GenerateContext) -> HclFile:
        return generate(resolved, ctx)
