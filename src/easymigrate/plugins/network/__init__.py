"""Networking plugin: VPCs, subnets, routes, NAT gateways, security groups and network ACLs."""

from typing import Any

from easymigrate.core.plugin import CollectContext, GenerateContext, NormalizeContext, Snapshot
from easymigrate.ir.base import IntentObject, IRDocument
from easymigrate.plugins.network.collect_aws import collect
from easymigrate.plugins.network.generate_gcp import generate
from easymigrate.plugins.network.normalize import normalize
from easymigrate.providers.gcp.hcl import HclFile


class NetworkPlugin:
    name = "network"
    aws_resource_types = frozenset(
        {
            "ec2:vpc",
            "ec2:subnet",
            "ec2:route-table",
            "ec2:internet-gateway",
            "ec2:natgateway",
            "ec2:security-group",
            "ec2:network-acl",
        }
    )

    def collect_aws(self, connection: Any, ctx: CollectContext) -> dict[str, Any]:
        return collect(connection, ctx)

    def normalize(self, snapshot: Snapshot, ctx: NormalizeContext) -> list[IntentObject]:
        return normalize(snapshot, ctx)

    def generate_gcp(self, resolved: IRDocument, ctx: GenerateContext) -> HclFile:
        return generate(resolved, ctx)
