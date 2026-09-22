import json
from datetime import datetime
from types import SimpleNamespace

import pytest
from moto import mock_aws

from easymigrate.core.errors import SourceConnectionError
from easymigrate.core.pipeline import analyze, scan
from easymigrate.core.plugin import SCAN_FILE
from easymigrate.core.project import Project, StageInputMissing
from easymigrate.core.registry import PLUGINS, source_for
from easymigrate.ir import SourceRef
from easymigrate.ir.network import Network


class FakeSource:
    def __init__(self, arns=None, denied=False, error=None):
        self.arns, self.denied, self.error = arns or {}, denied, error
        self.connected = None

    def connect(self, *, region, profile=None, role_arn=None):
        if self.error:
            raise self.error
        self.connected = (region, profile, role_arn)
        return SimpleNamespace(account_id="123456789012")

    def account_id(self, connection):
        return connection.account_id

    def inventory(self, connection):
        return self.arns, self.denied


class FakePlugin:
    aws_resource_types = frozenset({"ec2:vpc"})

    def __init__(self, name="fake", files=None, deny=None, make_objects=None, requires=frozenset()):
        self.name, self.files, self.deny, self.make_objects = name, files or {}, deny, make_objects
        self.requires = requires
        self.seen_objects = None
        self.snapshot_names = None

    def collect_aws(self, connection, ctx):
        if self.deny:
            ctx.access_denied(*self.deny)
        return dict(self.files)

    def normalize(self, snapshot, ctx):
        self.seen_objects = dict(ctx.objects)
        self.snapshot_names = snapshot.names()
        return self.make_objects(ctx) if self.make_objects else []


def network(name="main"):
    return Network(
        id=f"network:{name}",
        name=name,
        source=SourceRef(cloud="aws", type="vpc", id=f"vpc-{name}", region="ap-south-1"),
        cidr_blocks=["10.0.0.0/16"],
        dns_hostnames=True,
        dns_resolution=True,
    )


def test_registry_exposes_network_plugin_and_aws_source():
    names = [p.name for p in PLUGINS]
    assert "network" in names
    assert names[0] == "network", "network has no requirements and must run first"
    assert source_for("aws").__class__.__name__ == "AwsSource"
    with pytest.raises(ValueError, match="unsupported source cloud 'azure'"):
        source_for("azure")


def test_scan_writes_snapshot_scan_metadata_and_config(tmp_path):
    project = Project(tmp_path)
    source = FakeSource()
    plugin = FakePlugin(files={"fake.things": [{"id": 1}]}, deny=("DescribeX", "ec2:DescribeX"))

    summary = scan(project, region="ap-south-1", profile="prod", plugins=[plugin], source=source)

    assert source.connected == ("ap-south-1", "prod", None)
    assert summary.first_run is True
    assert summary.account_id == "123456789012" and summary.region == "ap-south-1"
    assert summary.files == ["fake.things"]
    snapshot_dir = project.snapshot_dir("aws")
    assert json.loads((snapshot_dir / "fake.things.json").read_text()) == [{"id": 1}]
    meta = json.loads((snapshot_dir / SCAN_FILE).read_text())
    assert meta["account_id"] == "123456789012" and meta["region"] == "ap-south-1"
    datetime.fromisoformat(meta["scanned_at"])
    assert [f["code"] for f in meta["findings"]] == ["SCAN-ACCESS-DENIED", "INVENTORY-TAGGED-ONLY"]
    config = project.load_config()
    assert (config.source.cloud, config.source.account_id, config.source.region) == (
        "aws",
        "123456789012",
        "ap-south-1",
    )
    assert scan(project, region="ap-south-1", plugins=[plugin], source=source).first_run is False


def test_scan_replaces_stale_snapshot_files(tmp_path):
    project = Project(tmp_path)
    project.init()
    stale = project.snapshot_dir("aws") / "network.gone.json"
    project.write_json(stale, [])

    scan(
        project,
        region="ap-south-1",
        plugins=[FakePlugin(files={"fake.new": []})],
        source=FakeSource(),
    )

    assert not stale.exists()
    assert (project.snapshot_dir("aws") / "fake.new.json").exists()


def test_failed_connection_keeps_previous_snapshot(tmp_path):
    project = Project(tmp_path)
    project.init()
    previous = project.snapshot_dir("aws") / "network.vpcs.json"
    project.write_json(previous, [])

    with pytest.raises(SourceConnectionError, match="no AWS credentials"):
        scan(
            project,
            region="ap-south-1",
            plugins=[FakePlugin()],
            source=FakeSource(error=SourceConnectionError("no AWS credentials found")),
        )

    assert previous.exists()


def test_inventory_findings_for_unsupported_types(tmp_path):
    arns = {
        "ec2:vpc": ["arn:aws:ec2:ap-south-1:1:vpc/vpc-1"],
        "lambda:function": [f"arn:aws:lambda:ap-south-1:1:function:f{i}" for i in range(4)],
    }

    summary = scan(
        Project(tmp_path), region="ap-south-1", plugins=[FakePlugin()], source=FakeSource(arns)
    )

    unsupported = [f for f in summary.findings if f.level == "unsupported"]
    assert len(unsupported) == 1
    finding = unsupported[0]
    assert (finding.code, finding.subject) == ("UNSUPPORTED-RESOURCE", "lambda:function")
    assert finding.message.startswith("4 lambda:function resource(s)")
    assert finding.message.endswith("and 1 more")
    assert "function:f3" not in finding.message


def test_inventory_skips_resources_a_plugin_manages(tmp_path):
    plugin = FakePlugin()
    plugin.managed_arns = (("cloudwatch:alarm", r":alarm:TargetTracking-"),)
    arns = {
        "cloudwatch:alarm": [
            "arn:aws:cloudwatch:ap-south-1:1:alarm:TargetTracking-web-asg-AlarmHigh-1",
            "arn:aws:cloudwatch:ap-south-1:1:alarm:TargetTracking-web-asg-AlarmLow-2",
        ]
    }
    summary = scan(
        Project(tmp_path), region="ap-south-1", plugins=[plugin], source=FakeSource(arns)
    )
    assert [f for f in summary.findings if f.level == "unsupported"] == []

    arns["cloudwatch:alarm"].append("arn:aws:cloudwatch:ap-south-1:1:alarm:disk-full")
    summary = scan(
        Project(tmp_path), region="ap-south-1", plugins=[plugin], source=FakeSource(arns)
    )
    unsupported = [f for f in summary.findings if f.level == "unsupported"]
    assert [f.message.split(" found")[0] for f in unsupported] == ["1 cloudwatch:alarm resource(s)"]


def test_inventory_access_denied_is_a_scan_gap(tmp_path):
    summary = scan(
        Project(tmp_path),
        region="ap-south-1",
        plugins=[FakePlugin()],
        source=FakeSource(denied=True),
    )

    assert [(f.level, f.code, f.suggestion) for f in summary.findings] == [
        ("scan_gap", "SCAN-ACCESS-DENIED", "Grant tag:GetResources")
    ]


def test_analyze_requires_scan(tmp_path):
    with pytest.raises(StageInputMissing, match="Run `easymigrate scan` first"):
        analyze(Project(tmp_path), plugins=[FakePlugin()])


def test_analyze_merges_findings_and_passes_objects_to_later_plugins(tmp_path):
    project = Project(tmp_path)
    first = FakePlugin(
        name="first",
        files={"first.data": []},
        deny=("DescribeX", "ec2:DescribeX"),
        make_objects=lambda ctx: (ctx.info("NET-DEFAULT-VPC", "default"), [network()])[1],
    )
    second = FakePlugin(name="second")
    scan(project, region="ap-south-1", plugins=[first, second], source=FakeSource())

    doc = analyze(project, plugins=[first, second])

    assert [o.id for o in doc.objects] == ["network:main"]
    assert second.seen_objects == {"network:main": doc.objects[0]}
    assert first.snapshot_names == ["first.data"]
    assert doc.source["account_id"] == "123456789012" and doc.source["cloud"] == "aws"
    assert "scanned_at" in doc.source
    assert [f.code for f in doc.findings] == [
        "SCAN-ACCESS-DENIED",
        "INVENTORY-TAGGED-ONLY",
        "NET-DEFAULT-VPC",
    ]
    assert project.read_ir(project.ir_path, produced_by="analyze").model_dump() == doc.model_dump()


def test_scan_and_analyze_end_to_end_with_moto(tmp_path, aws_credentials):
    project = Project(tmp_path)
    with mock_aws():
        ec2 = source_for("aws").connect(region="ap-south-1").client("ec2")
        vpc = ec2.create_vpc(
            CidrBlock="10.0.0.0/16",
            TagSpecifications=[{"ResourceType": "vpc", "Tags": [{"Key": "Name", "Value": "main"}]}],
        )["Vpc"]["VpcId"]
        ec2.create_subnet(VpcId=vpc, CidrBlock="10.0.1.0/24", AvailabilityZone="ap-south-1a")
        summary = scan(project, region="ap-south-1")

    doc = analyze(project, strict=True)

    assert "network.vpcs" in summary.files
    kinds = {o.kind for o in doc.objects}
    assert {"network", "subnet", "firewall_policy"} <= kinds
    assert "network:main" in [o.id for o in doc.objects]
    assert "NET-DEFAULT-VPC" in [f.code for f in doc.findings]
