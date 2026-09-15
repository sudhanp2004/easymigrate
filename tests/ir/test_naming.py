import re

import pytest

from easymigrate.ir.naming import allocate_ids, clean_labels, slug


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
    ids = allocate_ids("subnet", [("migration-test-subnet-1", "subnet-0a")])

    assert ids == {"subnet-0a": "subnet:migration-test-subnet-1"}


@pytest.mark.parametrize("name", [None, "", "日本"])
def test_allocate_falls_back_to_native_id(name):
    ids = allocate_ids("subnet", [(name, "subnet-0A")])

    assert ids == {"subnet-0A": "subnet:subnet-0a"}


def test_allocate_last_resort_is_unnamed():
    ids = allocate_ids("nat", [(None, "")])

    assert ids == {"": "nat:unnamed"}


def test_shared_name_gets_native_id_suffix():
    ids = allocate_ids("subnet", [("public", "subnet-2"), ("public", "subnet-1")])

    assert ids == {
        "subnet-2": "subnet:public-subnet-2",
        "subnet-1": "subnet:public-subnet-1",
    }


def test_shared_name_ids_are_order_independent():
    forward = allocate_ids("subnet", [("public", "subnet-2"), ("public", "subnet-1")])
    reverse = allocate_ids("subnet", [("public", "subnet-1"), ("public", "subnet-2")])

    assert forward == reverse


def test_shared_name_is_case_insensitive():
    ids = allocate_ids("subnet", [("Public", "subnet-2"), ("public", "subnet-1")])

    assert ids == {
        "subnet-2": "subnet:public-subnet-2",
        "subnet-1": "subnet:public-subnet-1",
    }


def test_single_name_keeps_plain_id_until_a_sibling_appears():
    ids = allocate_ids("subnet", [("public", "subnet-1")])

    assert ids == {"subnet-1": "subnet:public"}


def test_duplicate_native_id_raises():
    with pytest.raises(ValueError, match="subnet-1"):
        allocate_ids("subnet", [("a", "subnet-1"), ("b", "subnet-1")])


def test_forced_final_collision_raises():
    with pytest.raises(ValueError):
        allocate_ids(
            "subnet",
            [("a", "x-1"), ("a", "x-2"), ("a-x-1", "zzz")],
        )


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
