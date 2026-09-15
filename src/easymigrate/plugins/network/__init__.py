"""Networking plugin: VPCs, subnets, routes, NAT gateways, security groups and network ACLs."""

from typing import Any

from easymigrate.core.plugin import CollectContext, NormalizeContext, Snapshot
from easymigrate.ir.base import IntentObject
from easymigrate.plugins.network.collect_aws import collect
from easymigrate.plugins.network.normalize import normalize


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
