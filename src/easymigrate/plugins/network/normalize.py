"""Network plugin normalizer entry point: snapshot → networking IR objects."""

from easymigrate.core.plugin import NormalizeContext, Snapshot
from easymigrate.ir.base import IntentObject
from easymigrate.plugins.network.normalize_security import build_security
from easymigrate.plugins.network.normalize_topology import build_topology


def normalize(snapshot: Snapshot, ctx: NormalizeContext) -> list[IntentObject]:
    topology = build_topology(snapshot, ctx)
    policies, acls = build_security(snapshot, ctx, topology)
    return [
        *topology.networks,
        *topology.subnets,
        *topology.nats,
        *policies,
        *acls,
        *topology.routes,
    ]
