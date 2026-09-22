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
