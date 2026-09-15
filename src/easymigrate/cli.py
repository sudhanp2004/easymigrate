"""Command-line entry point. Commands only call `easymigrate.core`."""

import json
from collections import Counter
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, NoReturn

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from easymigrate import __version__
from easymigrate.core.errors import EasyMigrateError
from easymigrate.core.pipeline import analyze as run_analyze
from easymigrate.core.pipeline import scan as run_scan
from easymigrate.core.project import Project, StageInputMissing

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console(soft_wrap=True, highlight=False)
error_console = Console(stderr=True, soft_wrap=True, highlight=False)

METADATA_NOTE = (
    "Note: .easymigrate/ contains account metadata (account ID, ARNs, endpoints). Keep it private."
)
_LISTED_LEVELS = (("scan_gap", "Scan gaps"), ("unsupported", "Not yet supported"))


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f"easymigrate {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", callback=_print_version, is_eager=True, help="Show version and exit."
    ),
) -> None:
    """Migrate cloud infrastructure from AWS to GCP."""


def _fail(message: str) -> NoReturn:
    error_console.print(f"[red]Error:[/red] {message}")
    raise typer.Exit(code=1)


@contextmanager
def _user_errors() -> Iterator[None]:
    try:
        yield
    except (EasyMigrateError, StageInputMissing) as exc:
        _fail(str(exc))
    except ValidationError as exc:
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first["loc"])
        _fail(
            f"a project file is invalid or from an incompatible version "
            f"({exc.error_count()} error(s); first: {location}: {first['msg']})"
        )
    except json.JSONDecodeError as exc:
        _fail(f"a project file is not valid JSON: {exc.msg} (line {exc.lineno})")


def _print_findings(findings: Iterable[Any]) -> None:
    findings = list(findings)
    for level, title in _LISTED_LEVELS:
        selected = [f for f in findings if f.level == level]
        if not selected:
            continue
        console.print(f"[bold]{title} ({len(selected)})[/bold]")
        for finding in selected:
            line = f"  {finding.code}: {finding.message}"
            if finding.suggestion:
                line += f" → {finding.suggestion}"
            console.print(line, markup=False)
    notes = sum(1 for f in findings if f.level == "info")
    if notes:
        console.print(f"{notes} note(s) recorded in the project files.")


@app.command()
def scan(
    region: str | None = typer.Option(
        None, "--region", help="AWS region to scan, e.g. ap-south-1."
    ),
    aws_profile: str | None = typer.Option(None, "--aws-profile", help="AWS CLI profile to use."),
    aws_role_arn: str | None = typer.Option(
        None, "--aws-role-arn", help="Read-only IAM role to assume for the scan."
    ),
) -> None:
    """Scan a live AWS region into .easymigrate/snapshot/aws/."""
    if not region:
        _fail("pass --region, e.g. --region ap-south-1")
    project = Project(Path.cwd())
    with _user_errors():
        summary = run_scan(project, region=region, profile=aws_profile, role_arn=aws_role_arn)
    if summary.first_run:
        console.print(METADATA_NOTE, markup=False)
    console.print(
        f"Scanned AWS account {summary.account_id} in {summary.region}: "
        f"{len(summary.files)} snapshot file(s)."
    )
    _print_findings(summary.findings)
    console.print("Next: run `easymigrate analyze`.", markup=False)


@app.command()
def analyze() -> None:
    """Turn the latest scan into .easymigrate/ir.json."""
    project = Project(Path.cwd())
    with _user_errors():
        doc = run_analyze(project)
    counts = Counter(obj.kind for obj in doc.objects)
    table = Table("Kind", "Count", title="Intent objects")
    for kind in sorted(counts):
        table.add_row(kind, str(counts[kind]))
    console.print(table)
    _print_findings(doc.findings)
    console.print(f"Wrote {project.ir_path.relative_to(project.root)}.", markup=False)
