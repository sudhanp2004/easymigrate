"""Tests for `easymigrate resolve` (plan 3 task 4)."""

from __future__ import annotations

from moto import mock_aws
from typer.testing import CliRunner

from easymigrate.cli import app
from easymigrate.core.pipeline import analyze as run_analyze
from easymigrate.core.pipeline import scan
from easymigrate.core.project import Project
from easymigrate.core.registry import source_for

runner = CliRunner()


def _zone_regional_project(tmp_path) -> Project:
    project = Project(tmp_path)
    with mock_aws():
        ec2 = source_for("aws").connect(region="ap-south-1").client("ec2")
        vpc = ec2.create_vpc(
            CidrBlock="10.0.0.0/16",
            TagSpecifications=[{"ResourceType": "vpc", "Tags": [{"Key": "Name", "Value": "main"}]}],
        )["Vpc"]["VpcId"]
        ec2.create_subnet(VpcId=vpc, CidrBlock="10.0.1.0/24", AvailabilityZone="ap-south-1a")
        scan(project, region="ap-south-1")
    run_analyze(project)
    return project


def _manual_project(tmp_path) -> Project:
    project = Project(tmp_path)
    with mock_aws():
        ec2 = source_for("aws").connect(region="ap-south-1").client("ec2")
        vpc = ec2.create_vpc(
            CidrBlock="10.0.0.0/16",
            TagSpecifications=[{"ResourceType": "vpc", "Tags": [{"Key": "Name", "Value": "main"}]}],
        )["Vpc"]["VpcId"]
        ec2.create_network_acl(
            VpcId=vpc,
            TagSpecifications=[
                {"ResourceType": "network-acl", "Tags": [{"Key": "Name", "Value": "a"}]}
            ],
        )
        scan(project, region="ap-south-1")
    run_analyze(project)
    return project


def test_accept_defaults_with_manual_difference_exits_2_and_lists_it(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _manual_project(tmp_path)

    result = runner.invoke(app, ["resolve", "--accept-defaults"])

    assert result.exit_code == 2
    assert "NET-NACL" in result.output


def test_accept_defaults_with_acknowledge_manual_exits_0(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _manual_project(tmp_path)

    result = runner.invoke(app, ["resolve", "--accept-defaults", "--acknowledge-manual"])

    assert result.exit_code == 0
    assert "easymigrate plan" in result.output


def test_acknowledge_manual_alone_exits_1(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _manual_project(tmp_path)

    result = runner.invoke(app, ["resolve", "--acknowledge-manual"])

    assert result.exit_code == 1


def test_interactive_choose_strategy_then_enter_for_rest(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _zone_regional_project(tmp_path)

    # Moto's default VPC also triggers NET-ZONE-REGIONAL, so there are two differences,
    # sorted by key: "network:main" (ours) sorts before the default VPC's generated id.
    # Choose strategy 2 (merge_per_network) for ours, then Enter to keep the default for
    # the other.
    result = runner.invoke(app, ["resolve"], input="2\n\n")

    assert result.exit_code == 0
    project = Project(tmp_path)
    from easymigrate.differences.decisions import from_yaml

    decisions = from_yaml(project.decisions_path.read_text())
    assert decisions.decisions[0].strategy == "merge_per_network"
    assert decisions.decisions[0].decided_by == "user"


def test_interactive_enter_keeps_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _zone_regional_project(tmp_path)

    result = runner.invoke(app, ["resolve"], input="\n\n")

    assert result.exit_code == 0
    project = Project(tmp_path)
    from easymigrate.differences.decisions import from_yaml

    decisions = from_yaml(project.decisions_path.read_text())
    assert decisions.decisions[0].strategy == "keep_1to1"
    assert decisions.decisions[0].decided_by == "default"


def _merge_and_isolation_project(tmp_path) -> Project:
    """One network with a public and an isolated subnet, via a direct IR write so both
    NET-ZONE-REGIONAL and NET-ISOLATION-VM-LEVEL fire together."""
    from easymigrate.core.pipeline import _write_differences
    from easymigrate.ir.base import IRDocument, SourceRef
    from easymigrate.ir.network import Network, Subnet

    project = Project(tmp_path)
    project.init()
    network = Network(
        id="network:main",
        name="main",
        source=SourceRef(cloud="aws", type="vpc", id="network:main", region="ap-south-1"),
        cidr_blocks=["10.0.0.0/16"],
        dns_hostnames=True,
        dns_resolution=True,
    )
    public = Subnet(
        id="subnet:pub",
        name="pub",
        source=SourceRef(cloud="aws", type="subnet", id="subnet:pub", region="ap-south-1"),
        network="network:main",
        cidr="10.0.1.0/24",
        zone="ap-south-1a",
        internet="public",
        map_public_ip=False,
    )
    isolated = Subnet(
        id="subnet:app",
        name="app",
        source=SourceRef(cloud="aws", type="subnet", id="subnet:app", region="ap-south-1"),
        network="network:main",
        cidr="10.0.2.0/24",
        zone="ap-south-1b",
        internet="isolated",
        map_public_ip=False,
    )
    doc = IRDocument(source={"cloud": "aws"}, objects=[network, public, isolated])
    project.write_ir(project.ir_path, doc)
    _write_differences(project, doc)
    return project


def test_resolve_warns_about_superseded_decisions_and_exits_0(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _merge_and_isolation_project(tmp_path)

    # Sorted by key: NET-ISOLATION-VM-LEVEL@subnet:app comes before NET-ZONE-REGIONAL@network:main.
    # Keep the isolation default, then choose merge_per_network for the zone difference.
    result = runner.invoke(app, ["resolve"], input="\n2\n")

    assert result.exit_code == 0
    assert "no longer apply" in result.output
    assert "NET-ISOLATION-VM-LEVEL@subnet:app" in result.output
    assert "choose a different strategy for NET-ZONE-REGIONAL@network:main" in result.output


def test_interactive_manual_answer_n_reasks(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _manual_project(tmp_path)

    # Sorted by key, NET-NACL comes first. Its only strategy is "manual" (index 1).
    # Answer "n" first, which re-asks the strategy choice; answer "1" then "y" to accept.
    # Moto's default VPC also triggers a (non-manual) NET-ZONE-REGIONAL difference; Enter
    # keeps its default.
    result = runner.invoke(app, ["resolve"], input="1\nn\n1\ny\n\n")

    assert result.exit_code == 0
    project = Project(tmp_path)
    from easymigrate.differences.decisions import from_yaml

    decisions = from_yaml(project.decisions_path.read_text())
    assert decisions.decisions[0].strategy == "manual"
    assert decisions.decisions[0].acknowledged is True
