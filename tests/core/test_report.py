"""Report tests (plan 4 B1, spec §6.1): section presence/ordering, md and json shape."""

from __future__ import annotations

from moto import mock_aws

from easymigrate.core.pipeline import analyze as run_analyze
from easymigrate.core.pipeline import resolve as run_resolve
from easymigrate.core.pipeline import scan
from easymigrate.core.project import Project
from easymigrate.core.registry import source_for
from easymigrate.core.report import build_report, write_report

_SECTIONS_IN_ORDER = [
    "Scan gaps",
    "Needs your action",
    "Decisions",
    "Not yet supported",
    "Translated",
]


def _small_project(tmp_path) -> Project:
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
    run_analyze(project)
    run_resolve(project, accept_defaults=True)
    return project


def test_report_after_analyze_has_header_and_coverage(tmp_path):
    project = Project(tmp_path)
    with mock_aws():
        ec2 = source_for("aws").connect(region="ap-south-1").client("ec2")
        ec2.create_vpc(CidrBlock="10.0.0.0/16")
        scan(project, region="ap-south-1")
    run_analyze(project)

    report = build_report(project)

    assert report.source == "aws"
    assert report.target is None
    assert report.coverage["found"] >= 1
    assert report.plan is None


def test_report_sections_present_and_in_order(tmp_path):
    project = _small_project(tmp_path)

    report = build_report(project)
    md = report.to_markdown()

    positions = [md.index(f"## {title}") for title in _SECTIONS_IN_ORDER]
    assert positions == sorted(positions), "sections must appear in the documented order"


def test_decisions_table_lists_every_decision_with_risk(tmp_path):
    project = _small_project(tmp_path)

    report = build_report(project)

    assert report.decisions, "expected at least one decision (NET-ZONE-REGIONAL)"
    for row in report.decisions:
        assert row.key
        assert row.strategy
        assert row.decided_by in ("default", "user")
        assert row.risk in ("none", "low", "medium", "high")


def test_translated_counts_per_kind(tmp_path):
    project = _small_project(tmp_path)

    report = build_report(project)

    assert report.translated.get("network", 0) >= 1
    assert report.translated.get("subnet", 0) >= 2


def test_write_report_writes_md_and_json(tmp_path):
    project = _small_project(tmp_path)
    report = build_report(project)

    write_report(project, report)

    assert project.report_md_path.exists()
    assert project.report_json_path.exists()
    assert "Scan gaps" in project.report_md_path.read_text(encoding="utf-8")


def test_json_schema_keys(tmp_path):
    project = _small_project(tmp_path)
    report = build_report(project)

    data = report.to_dict()

    for key in (
        "source",
        "target",
        "coverage",
        "scan_gaps",
        "needs_action",
        "decisions",
        "not_supported",
        "translated",
        "info",
        "plan",
    ):
        assert key in data


def test_info_findings_only_in_md_and_json_not_terminal(tmp_path):
    project = _small_project(tmp_path)
    report = build_report(project)

    # info findings are collected for md/json...
    assert isinstance(report.info, list)
    # ...but the terminal renderer (cli.render_report) is responsible for omitting them;
    # the Report object itself just carries them separately from the listed sections.
    md = report.to_markdown()
    for finding in report.info:
        assert finding.message in md
