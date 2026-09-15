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
