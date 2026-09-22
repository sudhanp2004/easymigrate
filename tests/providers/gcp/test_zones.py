"""Positional zone letters resolve to zones the destination region has."""

from easymigrate.core.regions import REGION_MAP
from easymigrate.providers.gcp.zones import ZONE_SUFFIXES, zone_suffix


def test_regions_that_do_not_start_at_a_map_the_first_letter_to_their_first_zone():
    assert zone_suffix("us-east1", "a") == ("b", False)
    assert zone_suffix("europe-west1", "c") == ("d", False)


def test_more_source_zones_than_the_region_has_wraps_and_says_so():
    assert zone_suffix("asia-south1", "c") == ("c", False)
    assert zone_suffix("asia-south1", "d") == ("a", True)
    assert zone_suffix("us-central1", "d") == ("f", False)


def test_unknown_region_assumes_a_b_c():
    assert zone_suffix("mars-north1", "b") == ("b", False)


def test_every_mapped_region_has_a_zone_table():
    assert set(REGION_MAP.values()) <= set(ZONE_SUFFIXES)
