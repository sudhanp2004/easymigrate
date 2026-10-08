"""AWS→GCP Kubernetes difference rules (spec 5 §5).

Rules decide the GKE shape and record it in `target_hints`; `plugins/kubernetes/generate_gcp.py`
renders it. GKE facts live in `gke_facts.py` beside this module.
"""

from __future__ import annotations

import ipaddress
import math
import re
from collections import Counter
from typing import Any

from easymigrate.differences.model import Difference, DifferenceRule, Risk, Strategy
from easymigrate.differences.rules.aws_gcp.compute import (
    _AcknowledgeOnly,
    _replace_object,
    _set_hint,
    _subject,
)
from easymigrate.differences.rules.aws_gcp.gce_facts import nearest_predefined
from easymigrate.differences.rules.aws_gcp.gke_facts import (
    ADDON_MAP,
    GKE_MINORS,
    LINUX_IMAGE,
    LOG_COMPONENTS,
    MIN_PODS_PREFIX,
    MIN_SERVICES_PREFIX,
    PODS_POOL,
    PODS_PREFIX,
    SERVICES_POOL,
    SERVICES_PREFIX,
    UBUNTU_IMAGE,
    WINDOWS_IMAGE,
    nearest_minor,
)
from easymigrate.ir.base import IRDocument
from easymigrate.ir.kubernetes import KubernetesCluster, NodePool
from easymigrate.ir.network import Network, Subnet

_WORLD = {"0.0.0.0/0", "::/0"}


def _clusters(ir: IRDocument) -> list[KubernetesCluster]:
    return sorted(ir.of_kind(KubernetesCluster), key=lambda o: o.id)


def _pools_of(ir: IRDocument, cluster_id: str) -> list[NodePool]:
    return sorted((p for p in ir.of_kind(NodePool) if p.cluster == cluster_id), key=lambda p: p.id)


def _slug(ir_id: str) -> str:
    return ir_id.split(":", 1)[1]


class K8sEmptyCluster(_AcknowledgeOnly, DifferenceRule):
    id = "K8S-EMPTY-CLUSTER"
    family = "kubernetes"
    title = "The GKE cluster is created empty"
    impact = (
        "EasyMigrate migrates the cluster, not what runs in it. No Deployments, Services, "
        "ConfigMaps, Secrets, custom resources, Helm releases or persistent volumes are read or "
        "copied: the Kubernetes API is never called."
    )
    strategies = (
        Strategy(
            id="note_only",
            title="Understood",
            effect=(
                "the cluster and its node pools are created with nothing deployed; every "
                "workload must be redeployed from its source of truth"
            ),
            risk="high",
        ),
    )
    default = "note_only"

    def detect(self, ir: IRDocument) -> list[Difference]:
        return [self.difference([c.id]) for c in _clusters(ir)]


class K8sVersion(DifferenceRule):
    id = "K8S-VERSION"
    family = "kubernetes"
    title = "The Kubernetes version and upgrade policy change hands"
    impact = (
        "EKS keeps a cluster on its minor until the user upgrades it or support ends. Every new "
        "GKE cluster is in a release channel and upgrades automatically; the Extended channel "
        "is the closest to EKS, holding a minor for longest."
    )
    strategies = (
        Strategy(
            id="pin_minor",
            title="Pin the same minor on the Extended channel",
            effect=(
                "the cluster starts on the source minor (or the nearest newer one GKE offers) in "
                "the Extended channel, which keeps a minor longest; patch versions still upgrade "
                "automatically, and the extended-support period is billed extra"
            ),
            risk="low",
        ),
        Strategy(
            id="regular_channel",
            title="Enrol in the Regular release channel",
            effect="GKE picks the version and upgrades control plane and nodes automatically",
            risk="medium",
        ),
    )
    default = "pin_minor"

    def detect(self, ir: IRDocument) -> list[Difference]:
        diffs = []
        for cluster in _clusters(ir):
            chosen, why = nearest_minor(cluster.version)
            diffs.append(
                self.difference(
                    [cluster.id],
                    source_version=cluster.version,
                    gke_version=chosen,
                    why=why,
                    offered=list(GKE_MINORS),
                )
            )
        return diffs

    def risk_for(self, diff: Difference, strategy_id: str) -> Risk:
        if strategy_id == "pin_minor" and diff.context.get("why") != "offered":
            return "medium"
        return self.strategy(strategy_id).risk

    def apply(self, ir, diff, strategy_id, params=None) -> IRDocument:
        doc, cluster = _subject(ir, diff)
        if cluster is None:
            return doc
        if strategy_id == "pin_minor":
            # Not "no channel": GKE refuses UNSPECIFIED for new customers (live run,
            # 2026-09-22), and every channel requires node auto-upgrade.
            updated = _set_hint(cluster, "version", diff.context["gke_version"])
            updated = _set_hint(updated, "release_channel", "EXTENDED")
            return _replace_object(doc, _set_hint(updated, "node_auto_upgrade", True))
        if strategy_id == "regular_channel":
            updated = _set_hint(cluster, "release_channel", "REGULAR")
            return _replace_object(doc, _set_hint(updated, "node_auto_upgrade", True))
        raise ValueError(f"unknown strategy {strategy_id!r}")


class K8sEndpoint(DifferenceRule):
    id = "K8S-ENDPOINT"
    family = "kubernetes"
    title = "The API endpoint's exposure is reproduced, not narrowed"
    impact = (
        "EKS exposes the Kubernetes API publicly, privately or both, with an optional CIDR "
        "allow-list. GKE has a public endpoint guarded by authorized networks, or a private one. "
        "EasyMigrate mirrors the source so kubectl keeps working from where it worked before."
    )
    strategies = (
        Strategy(
            id="mirror",
            title="Mirror the source exposure",
            effect=(
                "a public endpoint with the same allow-list as authorized networks, or a private "
                "endpoint when EKS was private-only"
            ),
            risk="low",
        ),
        Strategy(
            id="private_endpoint",
            title="Private endpoint only",
            effect=(
                "the API is reachable only from inside the VPC; kubectl from outside stops working"
            ),
            risk="medium",
        ),
    )
    default = "mirror"

    def detect(self, ir: IRDocument) -> list[Difference]:
        return [
            self.difference(
                [c.id],
                public=c.endpoint_public,
                private=c.endpoint_private,
                cidrs=c.public_access_cidrs,
                world_open=c.endpoint_public
                and (not c.public_access_cidrs or bool(set(c.public_access_cidrs) & _WORLD)),
            )
            for c in _clusters(ir)
        ]

    def risk_for(self, diff: Difference, strategy_id: str) -> Risk:
        if strategy_id == "mirror" and diff.context.get("world_open"):
            return "high"
        if strategy_id == "mirror" and any(":" in c for c in diff.context.get("cidrs", [])):
            return "medium"  # IPv6 allow-list entries cannot be carried
        return self.strategy(strategy_id).risk

    def apply(self, ir, diff, strategy_id, params=None) -> IRDocument:
        doc, cluster = _subject(ir, diff)
        if cluster is None:
            return doc
        # A private endpoint renders no authorized-networks block: an empty one would enable the
        # allow-list with nothing on it.
        if strategy_id == "private_endpoint":
            endpoint = {"private_endpoint": True, "authorized_networks": None}
        elif strategy_id == "mirror":
            if not diff.context["public"]:
                endpoint = {"private_endpoint": True, "authorized_networks": None}
            elif diff.context["world_open"]:
                # No authorized-networks block: the endpoint accepts every source, as it did.
                endpoint = {"private_endpoint": False, "authorized_networks": None}
            else:
                # GKE authorized networks are IPv4-only; IPv6 entries are dropped and stated.
                ipv4 = [c for c in diff.context["cidrs"] if ":" not in c]
                endpoint = {"private_endpoint": False, "authorized_networks": ipv4}
        else:
            raise ValueError(f"unknown strategy {strategy_id!r}")
        return _replace_object(doc, _set_hint(cluster, "endpoint", endpoint))


class K8sPrivateNodes(DifferenceRule):
    id = "K8S-PRIVATE-NODES"
    family = "kubernetes"
    title = "Nodes get no external IP unless you ask for one"
    impact = (
        "EKS nodes in public subnets get public addresses. A GKE node with no external IP "
        "reaches the internet only through Cloud NAT, so image pulls from public registries "
        "need NAT on the node subnet; Google's own registries are reached privately."
    )
    strategies = (
        Strategy(
            id="private_nodes",
            title="Private nodes",
            effect=(
                "nodes have no external IPs; pulling from Docker Hub or other public registries "
                "needs Cloud NAT on the node subnet"
            ),
            risk="medium",
        ),
        Strategy(
            id="public_nodes",
            title="Public nodes",
            effect=(
                "every node has an external IP, reachable from the internet as the firewall allows"
            ),
            risk="high",
        ),
    )
    default = "private_nodes"

    def detect(self, ir: IRDocument) -> list[Difference]:
        return [self.difference([c.id]) for c in _clusters(ir)]

    def apply(self, ir, diff, strategy_id, params=None) -> IRDocument:
        doc, cluster = _subject(ir, diff)
        if cluster is None:
            return doc
        if strategy_id not in {"private_nodes", "public_nodes"}:
            raise ValueError(f"unknown strategy {strategy_id!r}")
        endpoint = cluster.target_hints.get("endpoint") or {}
        if strategy_id == "public_nodes" and endpoint.get("private_endpoint"):
            raise ValueError(
                f"{cluster.id} has a private API endpoint, which GKE allows only with private "
                "nodes: choose private_nodes, or a public endpoint in K8S-ENDPOINT"
            )
        return _replace_object(
            doc, _set_hint(cluster, "private_nodes", strategy_id == "private_nodes")
        )


def _occupied(ir: IRDocument) -> list[str]:
    ranges = [cidr for net in ir.of_kind(Network) for cidr in net.cidr_blocks]
    ranges += [subnet.cidr for subnet in ir.of_kind(Subnet)]
    return sorted(set(ranges))


def _overlaps(cidr: str, taken: list[str]) -> bool:
    network = ipaddress.ip_network(cidr)
    return any(network.overlaps(ipaddress.ip_network(t)) for t in taken)


def _next_free(pool, prefix: int, taken: list[str]) -> str | None:
    for candidate in pool.subnets(new_prefix=prefix):
        if not _overlaps(str(candidate), taken):
            return str(candidate)
    return None


class K8sPodRanges(DifferenceRule):
    id = "K8S-POD-RANGES"
    family = "kubernetes"
    title = "Pods move from VPC addresses to a secondary range"
    impact = (
        "EKS's VPC CNI gives every pod an address from the node's subnet. GKE takes pod and "
        "service addresses from secondary ranges on one subnet, so pods no longer hold VPC "
        "addresses: anything that allow-listed pod IPs, or relied on security groups for pods, "
        "must change."
    )
    strategies = (
        Strategy(
            id="secondary_ranges",
            title="Add pod and service ranges to the node subnet",
            effect=(
                "the chosen subnet gains a pods range and a services range; the EKS service "
                "CIDR is kept when it overlaps nothing, so in-cluster DNS keeps its address"
            ),
            risk="medium",
        ),
    )
    default = "secondary_ranges"
    param_docs = {
        "subnet": "IR id of the subnet the cluster and its node pools use",
        "pods_cidr": "pod range, /21 or larger, overlapping nothing in the network",
        "services_cidr": "service range, /27 or larger, overlapping nothing in the network",
    }

    def detect(self, ir: IRDocument) -> list[Difference]:
        occupied = _occupied(ir)
        allocated: list[str] = []
        diffs = []
        for cluster in _clusters(ir):
            counts = Counter(s for p in _pools_of(ir, cluster.id) for s in p.subnets)
            candidates = sorted(set(cluster.subnets) | set(counts))
            subnet = (
                sorted(candidates, key=lambda s: (-counts.get(s, 0), s))[0] if candidates else None
            )
            taken = occupied + allocated
            services = cluster.service_cidr
            if not services or _overlaps(services, taken):
                services = _next_free(SERVICES_POOL, SERVICES_PREFIX, taken)
            allocated.append(services)
            # A node takes a /24 of pod addresses (110 pods), so size the range for every node the
            # cluster may run; a fixed /16 ran out at 256 nodes (review, 2026-10-08).
            nodes = sum(p.max_size for p in _pools_of(ir, cluster.id)) or 1
            prefix = max(PODS_POOL.prefixlen, min(PODS_PREFIX, 24 - math.ceil(math.log2(nodes))))
            pods = _next_free(PODS_POOL, prefix, occupied + allocated)
            allocated.append(pods)
            diffs.append(
                self.difference(
                    [cluster.id],
                    subnet=subnet,
                    candidates=candidates,
                    pods_cidr=pods,
                    services_cidr=services,
                    source_service_cidr=cluster.service_cidr,
                    occupied=occupied,
                )
            )
        return diffs

    def default_params(self, diff: Difference, strategy_id: str) -> dict[str, Any]:
        return {
            "subnet": diff.context["subnet"],
            "pods_cidr": diff.context["pods_cidr"],
            "services_cidr": diff.context["services_cidr"],
        }

    def validate_params(self, diff: Difference, strategy_id: str, params: dict[str, Any]) -> None:
        if params.get("subnet") not in diff.context["candidates"]:
            raise ValueError(f"subnet must be one of {diff.context['candidates']}")
        chosen = []
        for key, minimum in (
            ("pods_cidr", MIN_PODS_PREFIX),
            ("services_cidr", MIN_SERVICES_PREFIX),
        ):
            try:
                network = ipaddress.ip_network(params.get(key) or "", strict=True)
            except ValueError as exc:
                raise ValueError(f"{key} must be a CIDR block: {exc}") from None
            if network.version != 4 or network.prefixlen > minimum:
                raise ValueError(f"{key} must be an IPv4 /{minimum} or larger")
            if _overlaps(str(network), diff.context["occupied"]):
                raise ValueError(f"{key} {network} overlaps a subnet or VPC range")
            chosen.append(network)
        if chosen[0].overlaps(chosen[1]):
            raise ValueError("pods_cidr and services_cidr overlap")

    def apply(self, ir, diff, strategy_id, params=None) -> IRDocument:
        doc, cluster = _subject(ir, diff)
        if cluster is None:
            return doc
        if strategy_id != "secondary_ranges":
            raise ValueError(f"unknown strategy {strategy_id!r}")
        params = {**self.default_params(diff, strategy_id), **(params or {})}
        if params["subnet"] is None:
            raise ValueError(f"{cluster.id} has no subnet to place its nodes in")
        self.validate_params(diff, strategy_id, params)
        slug = _slug(cluster.id)[:50]
        pods_name, services_name = f"{slug}-pods", f"{slug}-services"
        updated = _set_hint(cluster, "subnet", params["subnet"])
        updated = _set_hint(updated, "pods_range", pods_name)
        updated = _set_hint(updated, "services_range", services_name)
        doc = _replace_object(doc, updated)
        subnet = doc.get(params["subnet"])
        kept = [
            e
            for e in subnet.target_hints.get("secondary_ranges", [])
            if e["name"] not in {pods_name, services_name}
        ]
        ranges = [
            *kept,
            {"name": pods_name, "cidr": params["pods_cidr"]},
            {"name": services_name, "cidr": params["services_cidr"]},
        ]
        subnet = _set_hint(subnet, "secondary_ranges", ranges)
        # Nodes without external IPs reach Google's registries and APIs only this way; GKE
        # would otherwise switch it on itself and Terraform would see drift.
        return _replace_object(doc, _set_hint(subnet, "private_google_access", True))


class K8sLogging(DifferenceRule):
    id = "K8S-LOGGING"
    family = "kubernetes"
    title = "Control-plane log types map to GKE logging components"
    impact = (
        "EKS ships selected control-plane logs to CloudWatch. GKE sends component logs to Cloud "
        "Logging; audit logs are Cloud Audit Logs, and the authenticator log has no equivalent."
    )
    strategies = (
        Strategy(
            id="map_components",
            title="Map each log type",
            effect=(
                "api, controllerManager and scheduler become the matching GKE components, plus "
                "system components; container logs are not enabled, as on EKS"
            ),
            risk="low",
        ),
    )
    default = "map_components"

    def detect(self, ir: IRDocument) -> list[Difference]:
        diffs = []
        for cluster in _clusters(ir):
            unmapped = sorted(t for t in cluster.log_types if not LOG_COMPONENTS.get(t))
            diffs.append(
                self.difference([cluster.id], log_types=cluster.log_types, unmapped=unmapped)
            )
        return diffs

    def apply(self, ir, diff, strategy_id, params=None) -> IRDocument:
        doc, cluster = _subject(ir, diff)
        if cluster is None:
            return doc
        if strategy_id != "map_components":
            raise ValueError(f"unknown strategy {strategy_id!r}")
        components = {"SYSTEM_COMPONENTS"}
        components |= {LOG_COMPONENTS[t] for t in cluster.log_types if LOG_COMPONENTS.get(t)}
        return _replace_object(doc, _set_hint(cluster, "logging_components", sorted(components)))


class K8sSecretsEncryption(DifferenceRule):
    id = "K8S-SECRETS-ENCRYPTION"
    family = "kubernetes"
    title = "A KMS key for Kubernetes secrets does not carry to GCP"
    impact = (
        "EKS envelope-encrypts secrets with the customer's KMS key. GKE encrypts etcd at rest "
        "with Google-managed keys; application-layer encryption needs a Cloud KMS key."
    )
    strategies = (
        Strategy(
            id="google_managed",
            title="Google-managed encryption",
            effect="secrets are encrypted at rest by Google, without application-layer encryption",
            risk="low",
        ),
        Strategy(
            id="manual",
            title="Handle manually",
            effect="create a Cloud KMS key and set database_encryption on the cluster yourself",
            risk="low",
            manual=True,
        ),
    )
    default = "google_managed"

    def detect(self, ir: IRDocument) -> list[Difference]:
        return [
            self.difference([c.id], kms_key=c.secrets_kms_key)
            for c in _clusters(ir)
            if c.secrets_kms_key
        ]

    def apply(self, ir, diff, strategy_id, params=None) -> IRDocument:
        if strategy_id not in {"google_managed", "manual"}:
            raise ValueError(f"unknown strategy {strategy_id!r}")
        return ir.model_copy(deep=True)


class K8sAddons(DifferenceRule):
    id = "K8S-ADDONS"
    family = "kubernetes"
    title = "EKS add-ons map to GKE built-ins, or are not migrated"
    impact = (
        "The VPC CNI, kube-proxy and CoreDNS are part of every GKE cluster, and the Persistent "
        "Disk CSI driver replaces the EBS one. The EFS driver maps to Filestore. Any other add-on "
        "must be reinstalled; storage classes naming AWS provisioners must change either way."
    )
    strategies = (
        Strategy(
            id="map_addons",
            title="Map each add-on",
            effect="built-ins are used, Filestore CSI is enabled for EFS, and the rest are listed",
            risk="low",
        ),
    )
    default = "map_addons"

    def detect(self, ir: IRDocument) -> list[Difference]:
        diffs = []
        for cluster in _clusters(ir):
            if not cluster.addons:
                continue
            names = [a.name for a in cluster.addons]
            diffs.append(
                self.difference(
                    [cluster.id],
                    addons=names,
                    unmapped=sorted(n for n in names if n not in ADDON_MAP),
                )
            )
        return diffs

    def risk_for(self, diff: Difference, strategy_id: str) -> Risk:
        return "medium" if diff.context.get("unmapped") else "low"

    def apply(self, ir, diff, strategy_id, params=None) -> IRDocument:
        doc, cluster = _subject(ir, diff)
        if cluster is None:
            return doc
        if strategy_id != "map_addons":
            raise ValueError(f"unknown strategy {strategy_id!r}")
        enabled = sorted({ADDON_MAP[n] for n in diff.context["addons"] if ADDON_MAP.get(n)})
        return _replace_object(doc, _set_hint(cluster, "gke_addons", enabled))


class K8sIamForPods(_AcknowledgeOnly, DifferenceRule):
    id = "K8S-IAM-FOR-PODS"
    family = "kubernetes"
    title = "IAM roles for service accounts are not translated"
    impact = (
        "Pods that assumed AWS roles through IRSA or EKS Pod Identity lose those credentials. "
        "Workload Identity is enabled on the GKE cluster so each binding can be recreated as a "
        "Google service account, but EasyMigrate does not translate permissions."
    )
    strategies = (
        Strategy(
            id="manual",
            title="Recreate the bindings yourself",
            effect=(
                "Workload Identity is on; every listed service account needs a Google service "
                "account with equivalent access and an IAM binding"
            ),
            risk="high",
            manual=True,
        ),
    )
    default = "manual"

    def detect(self, ir: IRDocument) -> list[Difference]:
        return [
            self.difference(
                [c.id],
                oidc=bool(c.oidc_issuer),
                bindings=[f"{p.namespace}/{p.service_account}" for p in c.pod_identities],
            )
            for c in _clusters(ir)
            if c.oidc_issuer or c.pod_identities
        ]


class K8sClusterAccess(_AcknowledgeOnly, DifferenceRule):
    id = "K8S-CLUSTER-ACCESS"
    family = "kubernetes"
    title = "Who can use the cluster must be granted again"
    impact = (
        "EKS maps IAM principals to Kubernetes identities through access entries or the aws-auth "
        "ConfigMap. Neither carries: on GKE only Google IAM principals with container roles, "
        "plus in-cluster RBAC, grant access."
    )
    strategies = (
        Strategy(
            id="manual",
            title="Re-grant access yourself",
            effect=(
                "the new cluster is usable only by Google principals with container roles in "
                "the project; grant each team the roles and RBAC bindings it had"
            ),
            risk="medium",
            manual=True,
        ),
    )
    default = "manual"

    def detect(self, ir: IRDocument) -> list[Difference]:
        return [
            self.difference([c.id], access_entries=c.access_entries, mode=c.authentication_mode)
            for c in _clusters(ir)
        ]


class K8sIpv6(_AcknowledgeOnly, DifferenceRule):
    id = "K8S-IPV6"
    family = "kubernetes"
    title = "An IPv6 cluster is generated IPv4-only"
    impact = "GKE dual-stack needs subnet and cluster changes this family does not generate."
    strategies = (
        Strategy(
            id="manual",
            title="Handle manually",
            effect="the GKE cluster is IPv4; configure dual-stack yourself if pods need IPv6",
            risk="medium",
            manual=True,
        ),
    )
    default = "manual"

    def detect(self, ir: IRDocument) -> list[Difference]:
        return [self.difference([c.id]) for c in _clusters(ir) if c.ip_family == "ipv6"]


class K8sFargate(DifferenceRule):
    id = "K8S-FARGATE"
    family = "kubernetes"
    title = "Fargate pods run on ordinary nodes"
    impact = (
        "GKE Standard has no serverless pods. Pods a Fargate profile selected are scheduled on "
        "the cluster's node pools instead, which changes their isolation and billing. A cluster "
        "that ran only on Fargate, or on EKS Auto Mode, needs a node pool to run anything."
    )
    strategies = (
        Strategy(
            id="schedule_on_nodes",
            title="Schedule them on node pools",
            effect=(
                "the selected pods run on the cluster's nodes; when the cluster has no node "
                "groups, a default node pool is created"
            ),
            risk="medium",
        ),
    )
    default = "schedule_on_nodes"
    param_docs = {
        "machine_type": "machine type of the default node pool, e.g. e2-standard-4",
        "nodes_per_zone": "nodes per zone in the default node pool",
    }

    def detect(self, ir: IRDocument) -> list[Difference]:
        return [
            self.difference(
                [c.id],
                profiles=[p.name for p in c.fargate_profiles],
                namespaces=sorted({s.namespace for p in c.fargate_profiles for s in p.selectors}),
                has_pools=bool(_pools_of(ir, c.id)),
            )
            for c in _clusters(ir)
            if c.fargate_profiles or (c.auto_mode and not _pools_of(ir, c.id))
        ]

    def default_params(self, diff: Difference, strategy_id: str) -> dict[str, Any]:
        if diff.context.get("has_pools"):
            return {}
        return {"machine_type": "e2-standard-4", "nodes_per_zone": 1}

    def validate_params(self, diff: Difference, strategy_id: str, params: dict[str, Any]) -> None:
        if diff.context.get("has_pools"):
            return
        machine = params.get("machine_type")
        if not isinstance(machine, str) or not machine.strip():
            raise ValueError("machine_type must be a GCP machine type name")
        count = params.get("nodes_per_zone")
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 100:
            raise ValueError("nodes_per_zone must be an integer between 1 and 100")

    def apply(self, ir, diff, strategy_id, params=None) -> IRDocument:
        doc, cluster = _subject(ir, diff)
        if cluster is None:
            return doc
        if strategy_id != "schedule_on_nodes":
            raise ValueError(f"unknown strategy {strategy_id!r}")
        if diff.context.get("has_pools"):
            return doc
        params = {**self.default_params(diff, strategy_id), **(params or {})}
        self.validate_params(diff, strategy_id, params)
        pool = {"machine_type": params["machine_type"], "nodes_per_zone": params["nodes_per_zone"]}
        return _replace_object(doc, _set_hint(cluster, "default_pool", pool))


# --- Node pools (spec 5 plan 3) --------------------------------------------------------------

_MEMORY_DELTA_MEDIUM = 0.25


def _pools(ir: IRDocument) -> list[NodePool]:
    return sorted(ir.of_kind(NodePool), key=lambda p: p.id)


def node_zone_letters(ir: IRDocument) -> dict[str, str]:
    """AWS AZ -> positional letter across every node pool's subnets.

    Positional like `VM-ZONE-MAPPING`: pools that shared an AZ share a GCP zone, pools spread
    apart stay spread, and generation resolves each letter against the region's real zones.
    """
    subnets = {s.id: s for s in ir.of_kind(Subnet)}
    zones = sorted(
        {
            subnets[s].zone
            for p in ir.of_kind(NodePool)
            if p.excluded is None
            for s in p.subnets
            if s in subnets
        }
    )
    return {zone: chr(ord("a") + index) for index, zone in enumerate(zones)}


def _pool_zones(ir: IRDocument, pool: NodePool) -> list[str]:
    subnets = {s.id: s for s in ir.of_kind(Subnet)}
    return sorted({subnets[s].zone for s in pool.subnets if s in subnets})


class K8sNodeMachineType(DifferenceRule):
    id = "K8S-NODE-MACHINE-TYPE"
    family = "kubernetes"
    title = "A node group's instance type becomes one GKE machine type"
    impact = (
        "GKE node pools use one machine type, from fixed memory-per-vCPU ratios. A node group "
        "listing several instance types loses the others, and a ratio GCP has no type for gains "
        "memory rather than losing it."
    )
    strategies = (
        Strategy(
            id="nearest_predefined",
            title="Nearest predefined machine type",
            effect=(
                "same vCPU count, the smallest predefined type with at least the source's "
                "memory, chosen from the first instance type"
            ),
            risk="low",
        ),
        Strategy(
            id="custom_machine_type",
            title="Custom machine type",
            effect="exact vCPU and memory, at a higher per-unit price",
            risk="medium",
        ),
        Strategy(
            id="manual",
            title="Handle manually",
            effect="this node pool is not generated",
            risk="low",
            manual=True,
        ),
    )
    default = "nearest_predefined"

    def detect(self, ir: IRDocument) -> list[Difference]:
        diffs = []
        for pool in _pools(ir):
            primary = pool.instance_types[0] if pool.instance_types else ""
            chosen = nearest_predefined(pool.vcpus, pool.memory_mib, primary)
            diffs.append(
                self.difference(
                    [pool.id],
                    instance_types=pool.instance_types,
                    vcpus=pool.vcpus,
                    memory_mib=pool.memory_mib,
                    machine_type=chosen.name if chosen else None,
                    memory_delta_mib=(
                        chosen.memory_mib - pool.memory_mib if chosen and pool.memory_mib else None
                    ),
                )
            )
        return diffs

    def allowed_strategies(self, diff: Difference) -> tuple[Strategy, ...]:
        if diff.context.get("machine_type") is None:
            return (self.strategy("manual"),)
        return self.strategies

    def default_strategy(self, diff: Difference) -> str:
        return "manual" if diff.context.get("machine_type") is None else self.default

    def risk_for(self, diff: Difference, strategy_id: str) -> Risk:
        if strategy_id != "nearest_predefined":
            return self.strategy(strategy_id).risk
        if len(diff.context.get("instance_types") or []) > 1:
            return "medium"
        source, delta = diff.context.get("memory_mib"), diff.context.get("memory_delta_mib")
        if not source or delta is None:
            return "medium"
        return "medium" if delta / source >= _MEMORY_DELTA_MEDIUM else "low"

    def apply(self, ir, diff, strategy_id, params=None) -> IRDocument:
        doc, pool = _subject(ir, diff)
        if pool is None:
            return doc
        if strategy_id == "manual":
            return _replace_object(
                doc, pool.model_copy(update={"excluded": f"{self.id}: handled manually"})
            )
        if strategy_id == "custom_machine_type":
            if pool.vcpus is None or pool.memory_mib is None:
                raise ValueError("custom_machine_type needs vCPU and memory")
            return _replace_object(
                doc, _set_hint(pool, "machine_type", f"custom-{pool.vcpus}-{pool.memory_mib}")
            )
        if strategy_id != "nearest_predefined" or diff.context.get("machine_type") is None:
            raise ValueError(f"strategy {strategy_id!r} is not offered for {pool.id}")
        return _replace_object(doc, _set_hint(pool, "machine_type", diff.context["machine_type"]))


class K8sNodeImage(DifferenceRule):
    id = "K8S-NODE-IMAGE"
    family = "kubernetes"
    title = "EKS node AMIs become GKE node images"
    impact = (
        "EKS optimized AMIs (Amazon Linux, Bottlerocket) have no GKE counterpart; GKE nodes run "
        "Container-Optimized OS or Ubuntu. A custom AMI, and anything its bootstrap script "
        "configured, does not carry."
    )
    strategies = (
        Strategy(
            id="cos",
            title="Container-Optimized OS",
            effect="nodes run COS with containerd, GKE's default and most locked-down image",
            risk="low",
        ),
        Strategy(
            id="ubuntu",
            title="Ubuntu",
            effect="nodes run Ubuntu with containerd, for workloads that need a general distro",
            risk="low",
        ),
        Strategy(
            id="windows",
            title="Windows Server LTSC",
            effect="nodes run Windows Server with containerd; the cluster still needs a Linux pool",
            risk="medium",
        ),
    )
    default = "cos"

    def detect(self, ir: IRDocument) -> list[Difference]:
        return [
            self.difference(
                [p.id],
                ami_type=p.ami_type,
                windows=p.ami_type.upper().startswith("WINDOWS"),
                custom=p.ami_type.upper() == "CUSTOM",
            )
            for p in _pools(ir)
        ]

    def allowed_strategies(self, diff: Difference) -> tuple[Strategy, ...]:
        if diff.context.get("windows"):
            return (self.strategy("windows"),)
        return tuple(s for s in self.strategies if s.id != "windows")

    def default_strategy(self, diff: Difference) -> str:
        return "windows" if diff.context.get("windows") else self.default

    def risk_for(self, diff: Difference, strategy_id: str) -> Risk:
        if diff.context.get("custom"):
            return "medium"
        return self.strategy(strategy_id).risk

    def apply(self, ir, diff, strategy_id, params=None) -> IRDocument:
        doc, pool = _subject(ir, diff)
        if pool is None:
            return doc
        image = {"cos": LINUX_IMAGE, "ubuntu": UBUNTU_IMAGE, "windows": WINDOWS_IMAGE}.get(
            strategy_id
        )
        if image is None or strategy_id not in {s.id for s in self.allowed_strategies(diff)}:
            raise ValueError(f"strategy {strategy_id!r} is not offered for {pool.id}")
        return _replace_object(doc, _set_hint(pool, "image_type", image))


class K8sNodeScaling(DifferenceRule):
    id = "K8S-NODE-SCALING"
    family = "kubernetes"
    title = "Node counts are per zone in GKE, and its autoscaler is built in"
    impact = (
        "An EKS node group's sizes are totals, and it scales only if cluster-autoscaler or "
        "Karpenter is installed. A regional GKE node pool counts nodes per zone and has the "
        "cluster autoscaler built in, so the same bounds can behave differently."
    )
    strategies = (
        Strategy(
            id="autoscale_totals",
            title="Autoscale within the same totals",
            effect=(
                "GKE's autoscaler keeps the pool between the source minimum and maximum in "
                "total; it is on even if nothing scaled the node group on EKS"
            ),
            risk="medium",
        ),
        Strategy(
            id="fixed_size",
            title="Fixed size",
            effect="each zone runs a fixed number of nodes; nothing scales",
            risk="low",
        ),
    )
    default = "autoscale_totals"

    def detect(self, ir: IRDocument) -> list[Difference]:
        letters = node_zone_letters(ir)
        diffs = []
        for pool in _pools(ir):
            zones = _pool_zones(ir, pool)
            count = max(len(zones), 1)
            per_zone = math.ceil(pool.desired_size / count)
            # Autoscaling starts no higher than the maximum allows (review, 2026-10-08).
            initial = min(per_zone, pool.max_size // count)
            diffs.append(
                self.difference(
                    [pool.id],
                    zones=zones,
                    zone_letters=sorted({letters[z] for z in zones if z in letters}),
                    min_size=pool.min_size,
                    max_size=pool.max_size,
                    desired_size=pool.desired_size,
                    per_zone=per_zone,
                    initial_per_zone=initial,
                    # A regional pool runs at least `per_zone` nodes in every zone.
                    resulting_total=per_zone * max(len(zones), 1),
                )
            )
        return diffs

    def allowed_strategies(self, diff: Difference) -> tuple[Strategy, ...]:
        if diff.context["min_size"] == diff.context["max_size"]:
            return (self.strategy("fixed_size"),)
        return self.strategies

    def default_strategy(self, diff: Difference) -> str:
        if diff.context["min_size"] == diff.context["max_size"]:
            return "fixed_size"
        return self.default

    def risk_for(self, diff: Difference, strategy_id: str) -> Risk:
        # A regional pool runs at least one node per zone: say so when that changes the total.
        if (
            strategy_id == "fixed_size"
            and diff.context["resulting_total"] != diff.context["desired_size"]
        ):
            return "medium"
        return self.strategy(strategy_id).risk

    def apply(self, ir, diff, strategy_id, params=None) -> IRDocument:
        doc, pool = _subject(ir, diff)
        if pool is None:
            return doc
        if strategy_id not in {s.id for s in self.allowed_strategies(diff)}:
            raise ValueError(f"strategy {strategy_id!r} is not offered for {pool.id}")
        # Recomputed: a pool excluded by an earlier decision takes no zone.
        letters = node_zone_letters(doc)
        zone_letters = sorted({letters[z] for z in diff.context["zones"] if z in letters})
        updated = _set_hint(pool, "zone_letters", zone_letters)
        if strategy_id == "fixed_size":
            updated = _set_hint(updated, "node_count_per_zone", diff.context["per_zone"])
        else:
            updated = _set_hint(
                updated,
                "autoscaling",
                {
                    "total_min": pool.min_size,
                    "total_max": pool.max_size,
                    "initial_per_zone": diff.context.get(
                        "initial_per_zone", diff.context["per_zone"]
                    ),
                },
            )
        return _replace_object(doc, updated)


class K8sNodeSpot(DifferenceRule):
    id = "K8S-NODE-SPOT"
    family = "kubernetes"
    title = "Spot node groups become Spot node pools, preempted differently"
    impact = (
        "AWS gives Spot nodes a two-minute interruption notice; GKE Spot nodes get about thirty "
        "seconds. Handlers tuned for AWS may not finish draining."
    )
    strategies = (
        Strategy(id="spot", title="Spot node pool", effect="nodes are GKE Spot VMs", risk="medium"),
        Strategy(
            id="standard",
            title="Standard node pool",
            effect="nodes cost more and are never preempted",
            risk="low",
        ),
    )
    default = "spot"

    def detect(self, ir: IRDocument) -> list[Difference]:
        return [self.difference([p.id]) for p in _pools(ir) if p.capacity == "spot"]

    def apply(self, ir, diff, strategy_id, params=None) -> IRDocument:
        doc, pool = _subject(ir, diff)
        if pool is None:
            return doc
        if strategy_id == "standard":
            return doc
        if strategy_id != "spot":
            raise ValueError(f"unknown strategy {strategy_id!r}")
        return _replace_object(doc, _set_hint(pool, "spot", True))


class K8sSelfManagedNodes(DifferenceRule):
    id = "K8S-SELF-MANAGED-NODES"
    family = "kubernetes"
    title = "A self-managed node group becomes a node pool, without its bootstrap"
    impact = (
        "A self-managed node group joins the cluster through its own AMI and bootstrap script, "
        "which often set node labels and taints with --kubelet-extra-args. EasyMigrate reads "
        "none of that: the node pool gets the launch template's shape and nothing else, so "
        "workloads selecting those labels will not schedule until they are added."
    )
    strategies = (
        Strategy(
            id="node_pool",
            title="Create a node pool",
            effect=(
                "a GKE-managed node pool with the launch template's machine shape; custom AMI, "
                "bootstrap script and kubelet labels or taints are not carried"
            ),
            risk="high",
        ),
        Strategy(
            id="omit",
            title="Do not create it",
            effect="the capacity is reported and not created",
            risk="medium",
        ),
    )
    default = "node_pool"

    def detect(self, ir: IRDocument) -> list[Difference]:
        return [self.difference([p.id]) for p in _pools(ir) if p.origin == "self_managed"]

    def apply(self, ir, diff, strategy_id, params=None) -> IRDocument:
        doc, pool = _subject(ir, diff)
        if pool is None:
            return doc
        if strategy_id == "omit":
            return _replace_object(doc, pool.model_copy(update={"excluded": f"{self.id}: omitted"}))
        if strategy_id != "node_pool":
            raise ValueError(f"unknown strategy {strategy_id!r}")
        return doc


class K8sSshKey(_AcknowledgeOnly, DifferenceRule):
    id = "K8S-SSH-KEY"
    family = "kubernetes"
    title = "Node SSH keys do not carry"
    impact = "GKE nodes are reached with `gcloud compute ssh` and OS Login, not an EC2 key pair."
    strategies = (
        Strategy(
            id="note_only",
            title="Understood",
            effect="the key pair is not installed; use gcloud compute ssh to reach a node",
            risk="low",
        ),
    )
    default = "note_only"

    def detect(self, ir: IRDocument) -> list[Difference]:
        return [self.difference([p.id], ssh_key=p.ssh_key) for p in _pools(ir) if p.ssh_key]


_GRAVITON = re.compile(r"^[a-z]+\d+[a-z]*g[a-z]*\.")
_ACCELERATED = re.compile(r"^(g\d|p\d|inf\d|trn\d|dl\d)")


class K8sNodeSpecialHardware(_AcknowledgeOnly, DifferenceRule):
    id = "K8S-NODE-SPECIAL-HARDWARE"
    family = "kubernetes"
    title = "Arm and accelerator node groups need a deliberate choice"
    impact = (
        "The node group runs Arm (Graviton) or GPU/accelerator instances. EasyMigrate maps "
        "machine types to x86 predefined types without accelerators, so Arm-only images would "
        "not start and GPU workloads would find no GPU."
    )
    strategies = (
        Strategy(
            id="manual",
            title="Choose the hardware yourself",
            effect=(
                "the pool is generated as x86 with no accelerator; change its machine type to an "
                "Arm series (t2a, c4a) or add a guest_accelerator before applying"
            ),
            risk="high",
            manual=True,
        ),
    )
    default = "manual"

    def detect(self, ir: IRDocument) -> list[Difference]:
        diffs = []
        for pool in _pools(ir):
            ami = pool.ami_type.upper()
            types = pool.instance_types
            arm = "ARM" in ami or any(_GRAVITON.match(t) for t in types)
            accel = any(x in ami for x in ("GPU", "NVIDIA", "NEURON")) or any(
                _ACCELERATED.match(t) for t in types
            )
            if arm or accel:
                diffs.append(self.difference([pool.id], arm=arm, accelerator=accel))
        return diffs
