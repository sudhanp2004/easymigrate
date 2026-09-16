"""AWS region -> nearest GCP region mapping (plan 4 B2 step 1)."""

from __future__ import annotations

import pytest

from easymigrate.core.regions import REGION_MAP, gcp_region_for


def test_documented_examples():
    assert REGION_MAP["ap-south-1"] == "asia-south1"
    assert REGION_MAP["us-east-1"] == "us-east4"
    assert REGION_MAP["us-west-2"] == "us-west1"
    assert REGION_MAP["eu-west-1"] == "europe-west1"


def test_gcp_region_for_known_region_ignores_override():
    assert gcp_region_for("ap-south-1", override=None) == "asia-south1"


def test_gcp_region_for_uses_explicit_override_when_given():
    assert gcp_region_for("ap-south-1", override="asia-south2") == "asia-south2"


def test_unknown_region_without_override_raises():
    from easymigrate.core.errors import EasyMigrateError

    with pytest.raises(EasyMigrateError):
        gcp_region_for("mars-north-1", override=None)


def test_unknown_region_with_override_is_accepted():
    assert gcp_region_for("mars-north-1", override="us-central1") == "us-central1"


def test_every_commercial_region_is_mapped():
    # A representative sample of AWS commercial regions across every partition-1 area;
    # this is not exhaustive but guards against an obviously incomplete map.
    expected = [
        "us-east-1",
        "us-east-2",
        "us-west-1",
        "us-west-2",
        "ca-central-1",
        "eu-west-1",
        "eu-west-2",
        "eu-west-3",
        "eu-central-1",
        "eu-north-1",
        "ap-south-1",
        "ap-southeast-1",
        "ap-southeast-2",
        "ap-northeast-1",
        "ap-northeast-2",
        "sa-east-1",
    ]
    for region in expected:
        assert region in REGION_MAP, f"{region} missing from REGION_MAP"
