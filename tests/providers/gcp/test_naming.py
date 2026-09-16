import pytest

from easymigrate.providers.gcp.naming import (
    GCP_NAME_PATTERN,
    GcpNameAllocator,
    TfLabelAllocator,
    cloudsql_name,
    gcp_name,
    tf_label,
)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("migration-test-vpc", "migration-test-vpc"),
        ("Prod_VPC", "prod-vpc"),
        ("123", "r-123"),
        ("--edge--", "edge"),
        ("", "r"),
        ("日本", "r"),
    ],
)
def test_gcp_name(raw, expected):
    assert gcp_name(raw) == expected
    assert GCP_NAME_PATTERN.match(expected)


def test_gcp_name_truncates_to_63_without_trailing_dash():
    name = gcp_name("a" * 62 + "-bbbb")

    assert len(name) <= 63
    assert GCP_NAME_PATTERN.match(name)


def test_gcp_name_allocator_reports_renames_and_dedupes_per_type():
    names = GcpNameAllocator()

    assert names.allocate("google_compute_subnetwork", "public") == ("public", False)
    assert names.allocate("google_compute_subnetwork", "Public") == ("public-2", True)
    assert names.allocate("google_compute_network", "public") == ("public", False)


def test_gcp_name_allocator_suffix_respects_length_limit():
    names = GcpNameAllocator()
    long = "x" * 70

    first, _ = names.allocate("t", long)
    second, renamed = names.allocate("t", long)

    assert renamed is True
    assert first != second
    assert len(second) <= 63 and second.endswith("-2")
    assert GCP_NAME_PATTERN.match(second)


def test_cloudsql_name_allows_up_to_98_characters():
    name = cloudsql_name("a" * 100)

    assert len(name) == 98
    assert GCP_NAME_PATTERN.match(name) is None  # pattern caps at 63; check the rule by hand
    assert name[0].isalpha()
    assert all(c.isalnum() or c == "-" for c in name)


def test_cloudsql_name_keeps_short_names_unchanged_and_slugs_like_gcp_name():
    assert cloudsql_name("orders-db") == "orders-db"
    assert cloudsql_name("Prod_DB") == "prod-db"
    assert cloudsql_name("123") == "r-123"


def test_gcp_name_allocator_supports_a_wider_max_length():
    names = GcpNameAllocator(max_length=98)
    long = "x" * 100

    first, renamed_first = names.allocate("google_sql_database_instance", long)
    second, renamed_second = names.allocate("google_sql_database_instance", long)

    assert len(first) == 98
    assert renamed_first is True
    assert renamed_second is True
    assert first != second
    assert len(second) <= 98 and second.endswith("-2")


@pytest.mark.parametrize(
    "ir_id, expected",
    [
        ("subnet:migration-test-subnet-1", "migration_test_subnet_1"),
        ("network:10-net", "r_10_net"),
        ("firewall_policy:Web SG!", "web_sg"),
        ("nat:日本", "r"),
    ],
)
def test_tf_label(ir_id, expected):
    assert tf_label(ir_id) == expected


def test_tf_label_allocator_dedupes_per_resource_type():
    labels = TfLabelAllocator()

    assert labels.allocate("google_compute_subnetwork", "subnet:a-b") == "a_b"
    assert labels.allocate("google_compute_subnetwork", "subnet:a_b") == "a_b_2"
    assert labels.allocate("google_compute_router", "subnet:a-b") == "a_b"
