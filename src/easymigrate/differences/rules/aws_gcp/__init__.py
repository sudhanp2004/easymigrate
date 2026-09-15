"""AWS→GCP difference rule catalogue, in the fixed order rules apply (spec §4.4)."""

from __future__ import annotations

from easymigrate.differences.model import DifferenceRule
from easymigrate.differences.rules.aws_gcp.network import (
    NetCidrOverlap,
    NetCustomRoute,
    NetEgressTag,
    NetIcmpType,
    NetIpv6,
    NetIsolationVmLevel,
    NetNacl,
    NetPeering,
    NetPrefixList,
    NetSgXvpcRef,
    NetZoneRegional,
)

AWS_GCP_RULES: tuple[DifferenceRule, ...] = (
    NetZoneRegional(),
    NetIsolationVmLevel(),
    NetEgressTag(),
    NetSgXvpcRef(),
    NetPrefixList(),
    NetIcmpType(),
    NetNacl(),
    NetPeering(),
    NetCustomRoute(),
    NetIpv6(),
    NetCidrOverlap(),
)
