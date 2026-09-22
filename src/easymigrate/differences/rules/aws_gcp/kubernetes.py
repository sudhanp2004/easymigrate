"""AWS→GCP Kubernetes difference rules (spec 5 §5).

Rules decide the GKE shape and record it in `target_hints`; `plugins/kubernetes/generate_gcp.py`
renders it. GKE facts live in `gke_facts.py` beside this module.
"""

from __future__ import annotations

import ipaddress
from collections import Counter
from typing import Any

from easymigrate.differences.model import Difference, DifferenceRule, Risk, Strategy
from easymigrate.differences.rules.aws_gcp.compute import (
    _AcknowledgeOnly,
    _replace_object,
    _set_hint,
    _subject,
)
from easymigrate.differences.rules.aws_gcp.gke_facts import (
    ADDON_MAP,
    GKE_MINORS,
    LOG_COMPONENTS,
    MIN_PODS_PREFIX,
    MIN_SERVICES_PREFIX,
    PODS_POOL,
    PODS_PREFIX,
    SERVICES_POOL,
    SERVICES_PREFIX,
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
        "EKS keeps a cluster on its minor until the user upgrades it or standard support ends. "
        "GKE offers a set of minors and, in a release channel, upgrades automatically."
    )
    strategies = (
        Strategy(
            id="pin_minor",
            title="Pin the same minor, no release channel",
            effect=(
                "the cluster starts on the source minor (or the nearest newer one GKE offers) "
                "and node auto-upgrade is off; GKE still upgrades when the minor reaches end of "
                "support, as EKS does"
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
            updated = _set_hint(cluster, "version", diff.context["gke_version"])
            updated = _set_hint(updated, "release_channel", "UNSPECIFIED")
            return _replace_object(doc, _set_hint(updated, "node_auto_upgrade", False))
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
        return self.strategy(strategy_id).risk

    def apply(self, ir, diff, strategy_id, params=None) -> IRDocument:
        doc, cluster = _subject(ir, diff)
        if cluster is None:
            return doc
        if strategy_id == "private_endpoint":
            endpoint = {"private_endpoint": True, "authorized_networks": []}
        elif strategy_id == "mirror":
            if not diff.context["public"]:
                endpoint = {"private_endpoint": True, "authorized_networks": []}
            elif diff.context["world_open"]:
                # No authorized-networks block: the endpoint accepts every source, as it did.
                endpoint = {"private_endpoint": False, "authorized_networks": None}
            else:
                endpoint = {"private_endpoint": False, "authorized_networks": diff.context["cidrs"]}
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
            pods = _next_free(PODS_POOL, PODS_PREFIX, occupied + allocated)
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
        return _replace_object(doc, _set_hint(subnet, "secondary_ranges", ranges))


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
        "that ran only on Fargate needs a node pool to run anything."
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
            if c.fargate_profiles
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
