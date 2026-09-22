"""Hand-built EKS response shapes for kubernetes normalizer tests (spec 5 §4.2)."""

from __future__ import annotations

from typing import Any

from easymigrate.ir.base import SourceRef
from easymigrate.ir.network import FirewallPolicy, Network, Subnet


def network_objects() -> list:
    def source(type_: str, native: str) -> SourceRef:
        return SourceRef(cloud="aws", type=type_, id=native, region="ap-south-1")

    subnets = [
        Subnet(
            id=f"subnet:private-{i}",
            name=f"private-{i}",
            source=source("subnet", f"subnet-{i}"),
            network="network:vpc",
            cidr=f"10.0.{i}.0/24",
            zone=f"ap-south-1{'abc'[i - 1]}",
            internet="private_nat",
            map_public_ip=False,
        )
        for i in (1, 2, 3)
    ]
    policies = [
        FirewallPolicy(
            id=f"firewall_policy:{name}",
            name=name,
            source=source("security-group", native),
            network="network:vpc",
        )
        for name, native in (("cluster-sg", "sg-cluster"), ("nodes-sg", "sg-nodes"))
    ]
    network = Network(
        id="network:vpc",
        name="vpc",
        source=source("vpc", "vpc-1"),
        cidr_blocks=["10.0.0.0/16"],
        dns_hostnames=True,
        dns_resolution=True,
    )
    return [network, *subnets, *policies]


def cluster(name: str = "prod", **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": name,
        "version": "1.31",
        "status": "ACTIVE",
        "resourcesVpcConfig": {
            "subnetIds": ["subnet-1", "subnet-2"],
            "securityGroupIds": [],
            "clusterSecurityGroupId": "sg-cluster",
            "endpointPublicAccess": True,
            "endpointPrivateAccess": False,
            "publicAccessCidrs": ["0.0.0.0/0"],
        },
        "kubernetesNetworkConfig": {"serviceIpv4Cidr": "172.20.0.0/16", "ipFamily": "ipv4"},
        "logging": {"clusterLogging": [{"types": ["api", "audit"], "enabled": True}]},
        "identity": {"oidc": {"issuer": "https://oidc.eks.ap-south-1.amazonaws.com/id/ABC"}},
        "accessConfig": {"authenticationMode": "API_AND_CONFIG_MAP"},
        "tags": {"team": "platform"},
    }
    payload.update(overrides)
    return payload


def nodegroup(name: str = "workers", **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "nodegroupName": name,
        "clusterName": "prod",
        "status": "ACTIVE",
        "version": "1.31",
        "capacityType": "ON_DEMAND",
        "scalingConfig": {"minSize": 1, "maxSize": 4, "desiredSize": 2},
        "instanceTypes": ["m5.large"],
        "subnets": ["subnet-1", "subnet-2"],
        "amiType": "AL2023_x86_64_STANDARD",
        "diskSize": 50,
        "labels": {"role": "general"},
        "taints": [{"key": "dedicated", "value": "gpu", "effect": "NO_SCHEDULE"}],
        "updateConfig": {"maxUnavailable": 1},
        "tags": {},
    }
    payload.update(overrides)
    return payload


def snapshot_files(**files: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "kubernetes.clusters": {"prod": cluster()},
        "kubernetes.nodegroups": {"prod": [nodegroup()]},
        "kubernetes.fargate_profiles": {},
        "kubernetes.addons": {},
        "kubernetes.access_entries": {},
        "kubernetes.pod_identities": {},
        "kubernetes.self_managed_groups": {"AutoScalingGroups": []},
        "kubernetes.launch_templates": {},
        "kubernetes.instance_types": {"InstanceTypes": []},
    }
    base.update(files)
    return base
