import json

import pytest

from easymigrate.core.plugin import (
    SCAN_FILE,
    CollectContext,
    NormalizeContext,
    Snapshot,
    UnmodelledData,
)
from easymigrate.ir import SourceRef
from easymigrate.ir.network import Network


def test_snapshot_from_dir_skips_scan_file_and_non_json(tmp_path):
    (tmp_path / "network.vpcs.json").write_text(json.dumps([{"VpcId": "vpc-1"}]))
    (tmp_path / "network.subnets.json").write_text("[]")
    (tmp_path / SCAN_FILE).write_text("{}")
    (tmp_path / "notes.txt").write_text("ignore me")

    snapshot = Snapshot.from_dir(tmp_path)

    assert snapshot.names() == ["network.subnets", "network.vpcs"]
    assert snapshot.get("network.vpcs") == [{"VpcId": "vpc-1"}]
    assert snapshot.get("network.nat_gateways") is None


def test_snapshot_from_missing_dir_is_empty(tmp_path):
    assert Snapshot.from_dir(tmp_path / "absent").names() == []


def test_collect_context_records_access_denied_once_per_action():
    ctx = CollectContext(plugin="network")

    ctx.access_denied("DescribeVpcs", "ec2:DescribeVpcs")
    ctx.access_denied("DescribeVpcs", "ec2:DescribeVpcs")
    ctx.access_denied("DescribeSubnets", "ec2:DescribeSubnets")

    assert [f.suggestion for f in ctx.findings] == [
        "Grant ec2:DescribeVpcs",
        "Grant ec2:DescribeSubnets",
    ]
    first = ctx.findings[0]
    assert first.level == "scan_gap"
    assert first.code == "SCAN-ACCESS-DENIED"
    assert first.message == "Access denied calling DescribeVpcs; network data from it is missing."


def test_normalize_context_unmodelled_non_strict_records_info():
    ctx = NormalizeContext(account_id="123456789012", region="ap-south-1")

    ctx.unmodelled("IPv6-only subnet has no IPv4 CIDR", subject="subnet-1")

    assert len(ctx.findings) == 1
    finding = ctx.findings[0]
    assert (finding.level, finding.code, finding.subject) == (
        "info",
        "INTERNAL-UNMODELLED",
        "subnet-1",
    )
    assert finding.message == "IPv6-only subnet has no IPv4 CIDR"


def test_normalize_context_unmodelled_strict_raises():
    ctx = NormalizeContext(account_id="123456789012", region="ap-south-1", strict=True)

    with pytest.raises(UnmodelledData, match="IPv6-only subnet"):
        ctx.unmodelled("IPv6-only subnet has no IPv4 CIDR", subject="subnet-1")
    assert ctx.findings == []


def test_normalize_context_info_and_lookup():
    net = Network(
        id="network:main",
        name="main",
        source=SourceRef(cloud="aws", type="vpc", id="vpc-1", region="ap-south-1"),
        cidr_blocks=["10.0.0.0/16"],
        dns_hostnames=True,
        dns_resolution=True,
    )
    ctx = NormalizeContext(account_id="1", region="r", objects={net.id: net})

    ctx.info("NET-DEFAULT-VPC", "Default VPC", subject="network:main")

    assert ctx.lookup("network:main") is net
    assert ctx.findings[0].code == "NET-DEFAULT-VPC"
    with pytest.raises(KeyError):
        ctx.lookup("network:missing")
