import pytest

from easymigrate.plugins.network.aws_values import (
    associated_cidrs,
    cidr,
    icmp,
    name_tag,
    ports,
    protocol,
    tags,
)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("-1", "all"),
        (-1, "all"),
        ("6", "tcp"),
        ("tcp", "tcp"),
        ("TCP", "tcp"),
        ("17", "udp"),
        ("udp", "udp"),
        ("1", "icmp"),
        ("icmp", "icmp"),
        ("58", "icmpv6"),
        ("icmpv6", "icmpv6"),
        ("50", "50"),
    ],
)
def test_protocol(raw, expected):
    assert protocol(raw) == expected


@pytest.mark.parametrize(
    "proto, from_port, to_port, expected",
    [
        ("tcp", 443, 443, ["443"]),
        ("udp", 8000, 8080, ["8000-8080"]),
        ("tcp", 0, 65535, []),
        ("tcp", None, None, []),
        ("tcp", -1, -1, []),
        ("tcp", 22, None, ["22"]),
        ("icmp", 8, 0, []),
        ("all", None, None, []),
        ("50", 1, 2, []),
    ],
)
def test_ports(proto, from_port, to_port, expected):
    assert ports(proto, from_port, to_port) == expected


@pytest.mark.parametrize(
    "proto, type_value, code_value, expected",
    [
        ("icmp", 8, 0, (8, 0)),
        ("icmp", 8, -1, (8, None)),
        ("icmp", -1, -1, (None, None)),
        ("icmp", None, None, (None, None)),
        ("icmp", -1, 3, (None, None)),
        ("icmpv6", 128, None, (128, None)),
        ("tcp", 443, 443, (None, None)),
    ],
)
def test_icmp(proto, type_value, code_value, expected):
    assert icmp(proto, type_value, code_value) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("10.0.0.0/16", "10.0.0.0/16"),
        ("10.0.1.5/24", "10.0.1.0/24"),
        ("2600:1F18:ABCD:1200:0000::/56", "2600:1f18:abcd:1200::/56"),
        ("0.0.0.0/0", "0.0.0.0/0"),
    ],
)
def test_cidr_is_canonical(raw, expected):
    assert cidr(raw) == expected


def test_tags_and_name_tag():
    resource = {"Tags": [{"Key": "Name", "Value": "  web  "}, {"Key": "env", "Value": "prod"}]}

    assert tags(resource) == {"Name": "  web  ", "env": "prod"}
    assert name_tag(resource) == "web"
    assert name_tag({"Tags": [{"Key": "Name", "Value": "   "}]}) is None
    assert name_tag({}) is None
    assert tags({"Tags": None}) == {}


def test_associated_cidrs_keeps_only_associated_and_canonicalises():
    associations = [
        {"CidrBlock": "10.0.0.0/16", "CidrBlockState": {"State": "associated"}},
        {"CidrBlock": "10.1.0.0/16", "CidrBlockState": {"State": "disassociated"}},
        {"CidrBlock": "10.2.0.5/16", "CidrBlockState": {"State": "associated"}},
    ]

    assert associated_cidrs(associations, "CidrBlock", "CidrBlockState") == [
        "10.0.0.0/16",
        "10.2.0.0/16",
    ]
    assert associated_cidrs(None, "CidrBlock", "CidrBlockState") == []
