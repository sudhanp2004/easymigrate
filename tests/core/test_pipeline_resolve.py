"""Tests for pipeline.analyze writing differences.json and pipeline.resolve (plan 3 task 4)."""

from __future__ import annotations

import json

import pytest
from moto import mock_aws

from easymigrate.core.pipeline import ResolveSummary, load_differences, resolve, scan
from easymigrate.core.pipeline import analyze as run_analyze
from easymigrate.core.project import Project, StageInputMissing
from easymigrate.core.registry import source_for
from easymigrate.differences.decisions import from_yaml


def _two_subnet_vpc(tmp_path) -> Project:
    """One VPC with two subnets in different zones: triggers only NET-ZONE-REGIONAL."""
    project = Project(tmp_path)
    with mock_aws():
        ec2 = source_for("aws").connect(region="ap-south-1").client("ec2")
        vpc = ec2.create_vpc(
            CidrBlock="10.0.0.0/16",
            TagSpecifications=[{"ResourceType": "vpc", "Tags": [{"Key": "Name", "Value": "main"}]}],
        )["Vpc"]["VpcId"]
        ec2.create_subnet(VpcId=vpc, CidrBlock="10.0.1.0/24", AvailabilityZone="ap-south-1a")
        ec2.create_subnet(VpcId=vpc, CidrBlock="10.0.2.0/24", AvailabilityZone="ap-south-1b")
        scan(project, region="ap-south-1")
    return project


def _acl_vpc(tmp_path) -> Project:
    """One VPC with a custom network ACL: triggers only NET-NACL (a manual-only rule)."""
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
    return project


@pytest.fixture
def project_with_zone_regional(tmp_path):
    return _two_subnet_vpc(tmp_path)


@pytest.fixture
def project_with_manual_difference(tmp_path):
    return _acl_vpc(tmp_path)


def test_analyze_writes_differences_json_with_only_fired_rules(project_with_zone_regional):
    project = project_with_zone_regional
    run_analyze(project)

    data = json.loads(project.differences_path.read_text())
    assert {d["rule_id"] for d in data["differences"]} == {"NET-ZONE-REGIONAL"}
    assert "NET-ZONE-REGIONAL@network:main" in [d["key"] for d in data["differences"]]
    assert set(data["rules"]) == {"NET-ZONE-REGIONAL"}
    assert data["rules"]["NET-ZONE-REGIONAL"]["default"] == "keep_1to1"
    assert "strategies" in data["rules"]["NET-ZONE-REGIONAL"]


def test_load_differences_round_trips(project_with_zone_regional):
    project = project_with_zone_regional
    run_analyze(project)

    differences = load_differences(project)
    assert {d.rule_id for d in differences} == {"NET-ZONE-REGIONAL"}
    assert "NET-ZONE-REGIONAL@network:main" in [d.key for d in differences]


def test_resolve_before_analyze_raises_stage_input_missing(tmp_path):
    project = Project(tmp_path)
    project.init()
    with pytest.raises(StageInputMissing):
        resolve(project)


def test_resolve_with_accept_defaults_fills_in_defaults(project_with_zone_regional):
    project = project_with_zone_regional
    run_analyze(project)

    summary = resolve(project, accept_defaults=True)

    assert isinstance(summary, ResolveSummary)
    assert summary.dropped == []
    assert summary.problems == []
    assert summary.decisions.decisions[0].strategy == "keep_1to1"
    assert summary.decisions.decisions[0].decided_by == "default"
    saved = from_yaml(project.decisions_path.read_text())
    assert saved == summary.decisions


def test_resolve_choose_callback_records_user_decisions(project_with_zone_regional):
    project = project_with_zone_regional
    run_analyze(project)

    def choose(view):
        return "merge_per_network", False

    summary = resolve(project, choose=choose)

    decision = summary.decisions.decisions[0]
    assert decision.strategy == "merge_per_network"
    assert decision.decided_by == "user"


def test_resolve_accept_defaults_stops_on_unacknowledged_manual(project_with_manual_difference):
    project = project_with_manual_difference
    run_analyze(project)

    summary = resolve(project, accept_defaults=True)

    assert summary.decisions.decisions[0].strategy == "manual"
    assert summary.decisions.decisions[0].acknowledged is False
    assert summary.problems != []


def test_resolve_acknowledge_manual_sets_bulk_flag(project_with_manual_difference):
    project = project_with_manual_difference
    run_analyze(project)

    summary = resolve(project, accept_defaults=True, acknowledge_manual=True)

    assert summary.decisions.acknowledged_in_bulk is True
    manual_decision = next(d for d in summary.decisions.decisions if d.strategy == "manual")
    assert manual_decision.acknowledged is True
    assert summary.problems == []


def test_resolve_rerun_keeps_choices(project_with_zone_regional):
    project = project_with_zone_regional
    run_analyze(project)
    resolve(project, choose=lambda view: ("merge_per_network", False))

    summary = resolve(project, accept_defaults=True)

    assert summary.decisions.decisions[0].strategy == "merge_per_network"
    assert summary.decisions.decisions[0].decided_by == "user"
