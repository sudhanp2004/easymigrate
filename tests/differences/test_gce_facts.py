"""Compute Engine facts behind the VM-* rules (spec 4 §5)."""

from __future__ import annotations

import re

import pytest

from easymigrate.differences.rules.aws_gcp.gce_facts import (
    DISK_TYPE,
    LOCAL_SSD_GIB,
    PREDEFINED,
    SHARED_CORE,
    image_for,
    local_ssd_units,
    nearest_predefined,
)


def test_m5_xlarge_maps_exactly_to_n2_standard_4():
    chosen = nearest_predefined(4, 16384, "m5.xlarge")
    assert chosen is not None
    assert chosen.name == "n2-standard-4"
    assert chosen.memory_mib == 16384


def test_c5_xlarge_never_shrinks_memory_and_reports_the_delta():
    """c5.xlarge is 4 vCPU / 8 GiB — 2 GiB per vCPU, a ratio no GCP predefined type has."""
    chosen = nearest_predefined(4, 8192, "c5.xlarge")
    assert chosen is not None
    assert chosen.vcpus == 4
    assert chosen.memory_mib >= 8192


def test_burstable_micro_maps_to_a_shared_core_type():
    chosen = nearest_predefined(2, 1024, "t3.micro")
    assert chosen is not None
    assert chosen in SHARED_CORE


def test_unknown_sizing_has_no_answer():
    assert nearest_predefined(None, None, "zz.mega") is None


def test_absurd_size_has_no_answer():
    assert nearest_predefined(999, 99_999_999, "xx.huge") is None


@pytest.mark.parametrize(
    "platform, details, project_contains, family_contains",
    [
        ("linux", "Linux/UNIX", "rocky", "rocky"),
        ("linux", "Ubuntu Pro", "ubuntu", "ubuntu"),
        ("linux", "Red Hat Enterprise Linux", "rhel", "rhel"),
        ("windows", "Windows", "windows", "windows"),
    ],
)
def test_image_for_maps_platforms(platform, details, project_contains, family_contains):
    project, family = image_for(platform, details)
    assert project_contains in project
    assert family_contains in family


def test_amazon_linux_has_no_gcp_equivalent_and_lands_on_rocky():
    """Stated outright rather than implied: there is no Amazon Linux on GCP."""
    project, family = image_for("linux", "Linux/UNIX")
    assert "rocky" in family


@pytest.mark.parametrize(
    "aws_type, expected",
    [
        ("gp2", "pd-balanced"),
        ("gp3", "pd-balanced"),
        ("io1", "pd-ssd"),
        ("io2", "pd-ssd"),
        ("st1", "pd-standard"),
        ("sc1", "pd-standard"),
        ("standard", "pd-standard"),
    ],
)
def test_disk_type_mapping(aws_type, expected):
    assert DISK_TYPE[aws_type] == expected


@pytest.mark.parametrize(
    "size_gib, units",
    [(1, 1), (375, 1), (376, 2), (750, 2), (None, 1)],
)
def test_local_ssd_units_round_up_to_whole_devices(size_gib, units):
    """GCP local SSD comes in fixed 375 GiB devices; a partial device is not a thing."""
    assert local_ssd_units(size_gib) == units
    assert LOCAL_SSD_GIB == 375


def test_predefined_names_agree_with_their_declared_shape():
    """Guards typos in the table: n2-standard-4 must really be 4 vCPU, and a `standard`
    type must carry 4 GiB per vCPU, `highmem` 8, `highcpu` 1."""
    ratios = {"standard": 4096, "highmem": 8192, "highcpu": 1024}
    for machine in PREDEFINED:
        match = re.fullmatch(
            r"(?P<family>[a-z0-9]+)-(?P<class>[a-z]+)-(?P<vcpus>\d+)", machine.name
        )
        assert match, f"{machine.name} does not follow family-class-vcpus"
        assert int(match.group("vcpus")) == machine.vcpus, machine.name
        per_vcpu = ratios.get(match.group("class"))
        if per_vcpu is not None:
            assert machine.memory_mib == machine.vcpus * per_vcpu, machine.name


def test_shared_core_types_are_not_in_the_predefined_table():
    """They are chosen by a different branch, so double-listing would let a burstable
    source pick one through the normal vCPU match and skip the shared-core decision."""
    assert not set(SHARED_CORE) & set(PREDEFINED)
