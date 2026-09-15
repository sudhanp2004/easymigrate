import re

import pytest

from easymigrate.ir.naming import IdAllocator, clean_labels, slug


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("migration-test-vpc", "migration-test-vpc"),
        ("Prod VPC", "prod-vpc"),
        ("  weird__Name!!", "weird-name"),
        ("日本", ""),
        ("subnet-09125e7961d295170", "subnet-09125e7961d295170"),
    ],
)
def test_slug(raw, expected):
    assert slug(raw) == expected


def test_allocate_uses_name():
    assert IdAllocator().allocate("subnet", "migration-test-subnet-1", "subnet-0a") == (
        "subnet:migration-test-subnet-1"
    )


@pytest.mark.parametrize("name", [None, "", "日本"])
def test_allocate_falls_back_to_native_id(name):
    assert IdAllocator().allocate("subnet", name, "subnet-0A") == "subnet:subnet-0a"


def test_allocate_last_resort_is_unnamed():
    assert IdAllocator().allocate("nat", None, "") == "nat:unnamed"


def test_collisions_get_numeric_suffixes_per_kind():
    ids = IdAllocator()

    assert ids.allocate("subnet", "public", "subnet-1") == "subnet:public"
    assert ids.allocate("subnet", "Public", "subnet-2") == "subnet:public-2"
    assert ids.allocate("subnet", "public", "subnet-3") == "subnet:public-3"
    assert ids.allocate("network", "public", "vpc-1") == "network:public"


def test_clean_labels_follows_gcp_rules():
    tags = {
        "Name": "migration-test-vpc",
        "Cost Center": "R&D",
        "aws:cloudformation:stack-name": "stack",
        "123abc": "V",
        "!!!": "dropped",
    }

    assert clean_labels(tags) == {
        "name": "migration-test-vpc",
        "cost_center": "r_d",
        "tag_123abc": "v",
    }


def test_clean_labels_collisions_keep_first_sorted_key():
    assert clean_labels({"env": "b", "Env": "a"}) == {"env": "a"}


def test_clean_labels_truncates_and_caps_count():
    long = "x" * 100
    labels = clean_labels({f"k{i:03d}": long for i in range(70)})

    assert len(labels) == 64
    assert all(len(k) <= 63 and len(v) == 63 for k, v in labels.items())
    assert all(re.fullmatch(r"[a-z][a-z0-9_-]{0,62}", k) for k in labels)
