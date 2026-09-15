import json

from moto import mock_aws
from typer.testing import CliRunner

from easymigrate.cli import METADATA_NOTE, app

runner = CliRunner()


def test_scan_requires_region(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["scan"])

    assert result.exit_code == 1
    assert "pass --region" in result.output
    assert not (tmp_path / ".easymigrate").exists()


def test_scan_reports_connection_errors_without_traceback(tmp_path, monkeypatch, aws_credentials):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["scan", "--region", "mars-1"])

    assert result.exit_code == 1
    assert "Error: unknown AWS region 'mars-1'" in result.output
    assert "Traceback" not in result.output
    assert isinstance(result.exception, SystemExit)


def test_analyze_before_scan_names_the_command(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["analyze"])

    assert result.exit_code == 1
    assert "_scan.json not found. Run `easymigrate scan` first." in result.output


def test_corrupt_scan_file_is_a_clean_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    snapshot = tmp_path / ".easymigrate" / "snapshot" / "aws"
    snapshot.mkdir(parents=True)
    (snapshot / "_scan.json").write_text("{not json")

    result = runner.invoke(app, ["analyze"])

    assert result.exit_code == 1
    assert "not valid JSON" in result.output


def test_invalid_scan_findings_are_a_clean_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    snapshot = tmp_path / ".easymigrate" / "snapshot" / "aws"
    snapshot.mkdir(parents=True)
    (snapshot / "_scan.json").write_text(
        json.dumps(
            {
                "account_id": "1",
                "region": "ap-south-1",
                "scanned_at": "2026-09-15T00:00:00+00:00",
                "findings": [{"level": "approximated", "code": "X", "message": "m"}],
            }
        )
    )

    result = runner.invoke(app, ["analyze"])

    assert result.exit_code == 1
    assert "invalid" in result.output
    assert "Traceback" not in result.output


def test_scan_then_analyze(tmp_path, monkeypatch, aws_credentials):
    monkeypatch.chdir(tmp_path)

    with mock_aws():
        first = runner.invoke(app, ["scan", "--region", "ap-south-1"])
        second = runner.invoke(app, ["scan", "--region", "ap-south-1"])

    assert first.exit_code == 0, first.output
    assert METADATA_NOTE in first.output
    assert METADATA_NOTE not in second.output
    assert "Scanned AWS account 123456789012 in ap-south-1" in first.output
    assert "Next: run `easymigrate analyze`." in first.output

    result = runner.invoke(app, ["analyze"])

    assert result.exit_code == 0, result.output
    assert "Intent objects" in result.output
    assert "network" in result.output
    assert "Wrote .easymigrate/ir.json." in result.output
    assert (tmp_path / ".easymigrate" / "ir.json").exists()
