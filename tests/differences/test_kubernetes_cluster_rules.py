"""AWS→GCP Kubernetes cluster rules (spec 5 §5, plan 2)."""

from __future__ import annotations

import pytest

from easymigrate.differences.rules.aws_gcp import AWS_GCP_RULES
from easymigrate.differences.rules.aws_gcp.kubernetes import (
    K8sAddons,
    K8sClusterAccess,
    K8sEmptyCluster,
    K8sEndpoint,
    K8sFargate,
    K8sIamForPods,
    K8sIpv6,
    K8sLogging,
    K8sPodRanges,
    K8sPrivateNodes,
    K8sSecretsEncryption,
    K8sVersion,
)
from easymigrate.ir.base import IRDocument, SourceRef
from easymigrate.ir.kubernetes import (
    Addon,
    FargateProfile,
    FargateSelector,
    KubernetesCluster,
    NodePool,
    PodIdentity,
)
from tests.plugins.kubernetes.builders import network_objects


def _cluster(id_="kubernetes_cluster:prod", **kw):
    fields = dict(
        id=id_,
        name=id_.split(":", 1)[1],
        source=SourceRef(cloud="aws", type="eks-cluster", id="prod", region="ap-south-1"),
        version="1.31",
        subnets=["subnet:private-1", "subnet:private-2"],
        service_cidr="172.20.0.0/16",
    )
    fields.update(kw)
    return KubernetesCluster(**fields)


def _pool(
    id_="node_pool:workers", subnets=("subnet:private-2",), cluster="kubernetes_cluster:prod"
):
    return NodePool(
        id=id_,
        name=id_.split(":", 1)[1],
        source=SourceRef(cloud="aws", type="eks-nodegroup", id=id_, region="ap-south-1"),
        cluster=cluster,
        origin="managed",
        min_size=1,
        max_size=3,
        desired_size=2,
        subnets=list(subnets),
        ami_type="AL2_x86_64",
    )


def _doc(*objects):
    return IRDocument(source={"cloud": "aws"}, objects=[*network_objects(), *objects])


def _only(rule, doc):
    diffs = rule.detect(doc)
    assert len(diffs) == 1
    return diffs[0]


def _hints(doc, obj_id="kubernetes_cluster:prod"):
    return doc.get(obj_id).target_hints


def test_empty_cluster_is_always_stated_at_high_risk():
    rule = K8sEmptyCluster()
    diff = _only(rule, _doc(_cluster()))
    assert rule.risk_for(diff, "note_only") == "high"


@pytest.mark.parametrize(
    ("version", "chosen", "risk"),
    [("1.31", "1.31", "low"), ("1.27", "1.31", "medium"), ("1.40", "1.40", "medium")],
)
def test_version_pins_the_nearest_offered_minor(version, chosen, risk):
    rule = K8sVersion()
    doc = _doc(_cluster(version=version))
    diff = _only(rule, doc)
    assert rule.risk_for(diff, "pin_minor") == risk
    hints = _hints(rule.apply(doc, diff, "pin_minor"))
    assert hints == {
        "version": chosen,
        "release_channel": "UNSPECIFIED",
        "node_auto_upgrade": False,
    }


def test_regular_channel_drops_the_pin():
    rule = K8sVersion()
    doc = _doc(_cluster())
    hints = _hints(rule.apply(doc, _only(rule, doc), "regular_channel"))
    assert hints == {"release_channel": "REGULAR", "node_auto_upgrade": True}


def test_world_open_endpoint_is_mirrored_at_high_risk():
    rule = K8sEndpoint()
    doc = _doc(_cluster(public_access_cidrs=["0.0.0.0/0"]))
    diff = _only(rule, doc)
    assert rule.risk_for(diff, "mirror") == "high"
    assert _hints(rule.apply(doc, diff, "mirror"))["endpoint"] == {
        "private_endpoint": False,
        "authorized_networks": None,
    }


def test_allow_listed_endpoint_becomes_authorized_networks():
    rule = K8sEndpoint()
    doc = _doc(_cluster(public_access_cidrs=["203.0.113.0/24"]))
    diff = _only(rule, doc)
    assert rule.risk_for(diff, "mirror") == "low"
    endpoint = _hints(rule.apply(doc, diff, "mirror"))["endpoint"]
    assert endpoint == {"private_endpoint": False, "authorized_networks": ["203.0.113.0/24"]}


def test_private_only_endpoint_stays_private():
    rule = K8sEndpoint()
    doc = _doc(_cluster(endpoint_public=False, endpoint_private=True))
    endpoint = _hints(rule.apply(doc, _only(rule, doc), "mirror"))["endpoint"]
    assert endpoint["private_endpoint"] is True


def test_private_nodes_is_the_default():
    rule = K8sPrivateNodes()
    doc = _doc(_cluster())
    diff = _only(rule, doc)
    assert rule.default == "private_nodes"
    assert _hints(rule.apply(doc, diff, "private_nodes"))["private_nodes"] is True
    assert _hints(rule.apply(doc, diff, "public_nodes"))["private_nodes"] is False


def test_pod_ranges_keep_the_service_cidr_and_use_the_busiest_subnet():
    rule = K8sPodRanges()
    doc = _doc(_cluster(), _pool())
    diff = _only(rule, doc)
    assert diff.context["subnet"] == "subnet:private-2"  # hosts the node group
    assert diff.context["services_cidr"] == "172.20.0.0/16"
    assert diff.context["pods_cidr"] == "100.64.0.0/16"

    result = rule.apply(doc, diff, "secondary_ranges")
    assert _hints(result) == {
        "subnet": "subnet:private-2",
        "pods_range": "prod-pods",
        "services_range": "prod-services",
    }
    assert _hints(result, "subnet:private-2")["secondary_ranges"] == [
        {"name": "prod-pods", "cidr": "100.64.0.0/16"},
        {"name": "prod-services", "cidr": "172.20.0.0/16"},
    ]


def test_service_cidr_overlapping_the_vpc_is_reallocated():
    rule = K8sPodRanges()
    diff = _only(rule, _doc(_cluster(service_cidr="10.0.0.0/16")))
    assert diff.context["services_cidr"] == "100.96.0.0/20"


def test_two_clusters_get_disjoint_ranges():
    rule = K8sPodRanges()
    doc = _doc(_cluster(), _cluster("kubernetes_cluster:dev", service_cidr="172.20.0.0/16"))
    first, second = rule.detect(doc)
    assert {first.context["pods_cidr"], second.context["pods_cidr"]} == {
        "100.64.0.0/16",
        "100.65.0.0/16",
    }
    assert first.context["services_cidr"] != second.context["services_cidr"]


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"pods_cidr": "10.0.1.0/24"}, "pods_cidr"),
        ({"pods_cidr": "100.64.0.0/24"}, "/21"),
        ({"services_cidr": "100.64.0.0/20"}, "overlap"),
        ({"subnet": "subnet:private-3"}, "subnet"),
    ],
)
def test_pod_range_params_are_validated(params, message):
    rule = K8sPodRanges()
    diff = _only(rule, _doc(_cluster()))
    with pytest.raises(ValueError, match=message):
        rule.validate_params(diff, "secondary_ranges", {**rule.default_params(diff, ""), **params})


def test_logging_maps_types_and_always_adds_system_components():
    rule = K8sLogging()
    doc = _doc(_cluster(log_types=["api", "audit", "authenticator", "scheduler"]))
    diff = _only(rule, doc)
    assert diff.context["unmapped"] == ["audit", "authenticator"]
    assert _hints(rule.apply(doc, diff, "map_components"))["logging_components"] == [
        "APISERVER",
        "SCHEDULER",
        "SYSTEM_COMPONENTS",
    ]


def test_secrets_encryption_fires_only_with_a_kms_key():
    assert K8sSecretsEncryption().detect(_doc(_cluster())) == []
    assert len(K8sSecretsEncryption().detect(_doc(_cluster(secrets_kms_key="arn:kms")))) == 1


def test_addons_map_efs_to_filestore_and_flag_the_unknown():
    rule = K8sAddons()
    addons = [Addon(name="vpc-cni"), Addon(name="aws-efs-csi-driver"), Addon(name="adot")]
    doc = _doc(_cluster(addons=addons))
    diff = _only(rule, doc)
    assert rule.risk_for(diff, "map_addons") == "medium"
    assert _hints(rule.apply(doc, diff, "map_addons"))["gke_addons"] == ["filestore_csi"]


@pytest.mark.parametrize(
    ("rule", "cluster"),
    [
        (K8sIamForPods(), _cluster(oidc_issuer="https://oidc/x")),
        (
            K8sIamForPods(),
            _cluster(pod_identities=[PodIdentity(namespace="a", service_account="b")]),
        ),
        (K8sClusterAccess(), _cluster()),
        (K8sIpv6(), _cluster(ip_family="ipv6")),
    ],
)
def test_manual_cluster_rules_keep_the_cluster(rule, cluster):
    doc = _doc(cluster)
    diff = _only(rule, doc)
    assert rule.strategy(rule.default).manual is True
    assert rule.apply(doc, diff, "manual").get(cluster.id).excluded is None


def test_fargate_only_cluster_gets_a_default_pool():
    rule = K8sFargate()
    profile = FargateProfile(name="batch", selectors=[FargateSelector(namespace="batch")])
    doc = _doc(_cluster(fargate_profiles=[profile]))
    diff = _only(rule, doc)
    assert diff.context["namespaces"] == ["batch"]
    params = rule.default_params(diff, "schedule_on_nodes")
    assert _hints(rule.apply(doc, diff, "schedule_on_nodes", params))["default_pool"] == {
        "machine_type": "e2-standard-4",
        "nodes_per_zone": 1,
    }
    with pytest.raises(ValueError):
        rule.validate_params(diff, "schedule_on_nodes", {**params, "nodes_per_zone": 0})


def test_fargate_with_node_groups_adds_no_pool():
    rule = K8sFargate()
    profile = FargateProfile(name="batch", selectors=[FargateSelector(namespace="batch")])
    doc = _doc(_cluster(fargate_profiles=[profile]), _pool())
    diff = _only(rule, doc)
    assert rule.default_params(diff, "schedule_on_nodes") == {}
    assert "default_pool" not in _hints(rule.apply(doc, diff, "schedule_on_nodes"))


def test_cluster_rules_run_after_compute_and_version_first():
    ids = [rule.id for rule in AWS_GCP_RULES]
    k8s = [i for i in ids if i.startswith("K8S-")]
    assert ids.index("VM-ASG-LIFECYCLE") < ids.index(k8s[0])
    assert k8s.index("K8S-VERSION") < k8s.index("K8S-POD-RANGES") < k8s.index("K8S-FARGATE")
