"""ComputePlugin wiring (spec 4 §2.2, §3, §4.1)."""

from easymigrate.core import registry
from easymigrate.core.plugin import Plugin
from easymigrate.ir.base import IRDocument
from easymigrate.plugins.compute import ComputePlugin
from easymigrate.plugins.compute.collect_aws import COMPUTE_SNAPSHOT_FILES


def test_compute_plugin_satisfies_the_protocol():
    plugin: Plugin = ComputePlugin()
    assert plugin.name == "compute"


def test_generate_is_an_empty_file_until_plan_3():
    empty = IRDocument(source={"cloud": "aws", "account_id": "1", "region": "ap-south-1"})
    assert ComputePlugin().generate_gcp(empty, None).render() == ""


def test_compute_is_registered_after_network():
    names = [plugin.name for plugin in registry.PLUGINS]
    assert "compute" in names
    assert names.index("network") < names.index("compute")


def test_requires_declares_the_network_dependency():
    assert ComputePlugin().requires == frozenset({"network"})


def test_every_claimed_resource_type_is_one_this_family_collects():
    """`aws_resource_types` drives the inventory sweep: over-claiming hides a gap, and
    under-claiming makes the scan warn about resources this family does migrate."""
    claimed = ComputePlugin().aws_resource_types
    assert claimed == {
        "ec2:instance",
        "ec2:volume",
        "ec2:elastic-ip",
        "ec2:launch-template",
        "autoscaling:autoScalingGroup",
    }


def test_no_other_plugin_claims_the_same_resource_types():
    seen: set[str] = set()
    for plugin in registry.PLUGINS:
        overlap = seen & plugin.aws_resource_types
        assert not overlap, f"{plugin.name} re-claims {sorted(overlap)}"
        seen |= plugin.aws_resource_types


def test_snapshot_file_names_are_all_compute_prefixed():
    """`Snapshot.from_dir` keys by file name, so a collision with another family's files
    would silently overwrite them."""
    assert all(name.startswith("compute.") for name in COMPUTE_SNAPSHOT_FILES)
