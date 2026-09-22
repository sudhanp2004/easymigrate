"""Kubernetes → GKE generation (spec 5 §6)."""

from __future__ import annotations

import pytest

from easymigrate.core.plugin import GenerateContext, GenerationError
from easymigrate.ir.base import IRDocument, SourceRef
from easymigrate.ir.kubernetes import KubernetesCluster
from easymigrate.plugins.kubernetes.generate_gcp import generate
from easymigrate.plugins.network.generate_gcp import generate as generate_network
from tests.plugins.kubernetes.builders import network_objects

CLUSTER_HINTS = {
    "version": "1.31",
    "release_channel": "UNSPECIFIED",
    "node_auto_upgrade": False,
    "subnet": "subnet:private-1",
    "pods_range": "prod-pods",
    "services_range": "prod-services",
    "private_nodes": True,
    "endpoint": {"private_endpoint": False, "authorized_networks": ["203.0.113.0/24"]},
    "logging_components": ["APISERVER", "SYSTEM_COMPONENTS"],
}


def _ctx():
    return GenerateContext(project="p", region="asia-south1", version="test")


def _cluster(**hints):
    return KubernetesCluster(
        id="kubernetes_cluster:prod",
        name="prod",
        source=SourceRef(cloud="aws", type="eks-cluster", id="prod", region="ap-south-1"),
        version="1.31",
        target_hints={**CLUSTER_HINTS, **hints},
    )


def _render(*objects):
    return generate(
        IRDocument(source={"cloud": "aws"}, objects=[*network_objects(), *objects]), _ctx()
    ).render()


def test_cluster_renders_regional_vpc_native_and_removable():
    out = _render(_cluster())
    assert 'resource "google_container_cluster" "prod"' in out
    assert "location                 = var.region" in out
    assert "subnetwork               = google_compute_subnetwork.private_1.id" in out
    assert "remove_default_node_pool = true" in out
    assert "deletion_protection      = false" in out
    assert 'min_master_version       = "1.31"' in out
    assert 'cluster_secondary_range_name  = "prod-pods"' in out
    assert 'workload_pool = "${var.project}.svc.id.goog"' in out


def test_endpoint_and_private_nodes_render():
    out = _render(_cluster())
    assert "enable_private_nodes    = true" in out
    assert "enable_private_endpoint = false" in out
    assert 'cidr_block   = "203.0.113.0/24"' in out


def test_world_open_endpoint_renders_no_authorized_networks():
    endpoint = {"private_endpoint": False, "authorized_networks": None}
    out = _render(_cluster(endpoint=endpoint, private_nodes=False))
    assert "master_authorized_networks_config" not in out
    assert "private_cluster_config" not in out


def test_regular_channel_has_no_version_pin():
    out = _render(_cluster(release_channel="REGULAR", version=None))
    assert "min_master_version" not in out
    assert 'channel = "REGULAR"' in out


def test_filestore_addon_renders():
    out = _render(_cluster(gke_addons=["filestore_csi"]))
    assert "gcp_filestore_csi_driver_config" in out


def test_excluded_cluster_is_not_rendered():
    cluster = _cluster().model_copy(update={"excluded": "manual"})
    assert "google_container_cluster" not in _render(cluster)


@pytest.mark.parametrize("hint", ["subnet", "release_channel", "pods_range", "services_range"])
def test_missing_hint_is_a_generation_error(hint):
    cluster = _cluster()
    hints = dict(cluster.target_hints)
    del hints[hint]
    with pytest.raises(GenerationError, match=hint):
        _render(cluster.model_copy(update={"target_hints": hints}))


def test_subnet_renders_the_secondary_ranges_a_cluster_chose():
    objects = network_objects()
    ranges = [
        {"name": "prod-services", "cidr": "172.20.0.0/16"},
        {"name": "prod-pods", "cidr": "100.64.0.0/16"},
    ]
    objects = [
        o.model_copy(update={"target_hints": {"secondary_ranges": ranges}})
        if o.id == "subnet:private-1"
        else o
        for o in objects
    ]
    out = generate_network(IRDocument(source={"cloud": "aws"}, objects=objects), _ctx()).render()
    block = out.split('resource "google_compute_subnetwork" "private_1"')[1].split("resource")[0]
    assert block.index("prod-pods") < block.index("prod-services")
    assert 'ip_cidr_range = "100.64.0.0/16"' in block


# ---- node pools ------------------------------------------------------------------

from easymigrate.ir.kubernetes import NodePool, Taint  # noqa: E402

POOL_HINTS = {
    "machine_type": "n2-standard-2",
    "image_type": "COS_CONTAINERD",
    "zone_letters": ["a", "b"],
    "autoscaling": {"total_min": 1, "total_max": 4, "initial_per_zone": 2},
}


def _pool(**hints):
    return NodePool(
        id="node_pool:workers",
        name="workers",
        source=SourceRef(cloud="aws", type="eks-nodegroup", id="prod/workers", region="x"),
        cluster="kubernetes_cluster:prod",
        origin="managed",
        min_size=1,
        max_size=4,
        desired_size=3,
        ami_type="AL2_x86_64",
        disk_size_gib=8,
        node_labels={"role": "general"},
        taints=[Taint(key="dedicated", value="gpu", effect="NO_SCHEDULE")],
        firewall_policies=["firewall_policy:cluster-sg"],
        max_unavailable=1,
        target_hints={**POOL_HINTS, **hints},
    )


def test_node_pool_renders_zones_autoscaling_and_node_config():
    from easymigrate.plugins.network.generate_gcp import policy_network_tags

    out = _render(_cluster(), _pool())
    assert 'resource "google_container_node_pool" "workers"' in out
    assert "cluster            = google_container_cluster.prod.id" in out
    assert 'node_locations     = ["${var.region}-a", "${var.region}-b"]' in out
    assert "total_max_node_count = 4" in out
    assert 'location_policy      = "BALANCED"' in out
    assert "auto_upgrade = false" in out
    assert "disk_size_gb = 10" in out  # raised to GKE's minimum
    assert 'effect = "NO_SCHEDULE"' in out
    policy = next(o for o in network_objects() if o.id == "firewall_policy:cluster-sg")
    assert policy_network_tags([policy])[0][policy.id] in out


def test_fixed_size_pool_renders_node_count():
    pool = _pool(autoscaling=None, node_count_per_zone=1)
    hints = {k: v for k, v in pool.target_hints.items() if v is not None}
    out = _render(_cluster(), pool.model_copy(update={"target_hints": hints}))
    assert "node_count     = 1" in out
    assert "autoscaling" not in out


def test_spot_pool_renders_spot():
    assert "spot         = true" in _render(_cluster(), _pool(spot=True))


def test_fargate_default_pool_renders_for_a_cluster_with_no_node_groups():
    cluster = _cluster(default_pool={"machine_type": "e2-standard-4", "nodes_per_zone": 1})
    out = _render(cluster)
    assert 'resource "google_container_node_pool" "prod_fargate"' in out
    assert 'machine_type = "e2-standard-4"' in out


def test_pool_whose_cluster_is_excluded_is_a_generation_error():
    cluster = _cluster().model_copy(update={"excluded": "manual"})
    with pytest.raises(GenerationError, match="cannot exist without its cluster"):
        _render(cluster, _pool())


def test_pool_without_zones_is_a_generation_error():
    with pytest.raises(GenerationError, match="no zones"):
        _render(_cluster(), _pool(zone_letters=[]))
