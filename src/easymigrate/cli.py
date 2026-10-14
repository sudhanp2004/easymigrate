"""Command-line entry point. Commands only call `easymigrate.core`."""

import json
import socket
import sys
import webbrowser
from collections import Counter
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from easymigrate import __version__
from easymigrate.core.errors import EasyMigrateError, ExitTwoError, SchemaError
from easymigrate.core.pipeline import DestroySummary, PlanSummary, set_scope
from easymigrate.core.pipeline import analyze as run_analyze
from easymigrate.core.pipeline import apply as run_apply
from easymigrate.core.pipeline import destroy as run_destroy
from easymigrate.core.pipeline import plan as run_plan
from easymigrate.core.pipeline import resolve as run_resolve
from easymigrate.core.pipeline import scan as run_scan
from easymigrate.core.project import Project, StageInputMissing
from easymigrate.core.regions import gcp_region_for
from easymigrate.core.report import Report, build_report, write_report

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
    except ExitTwoError as exc:
        for line in str(exc).splitlines() or [str(exc)]:
            error_console.print(f"[red]{line}[/red]")
        raise typer.Exit(code=2) from None
    except (EasyMigrateError, StageInputMissing, SchemaError) as exc:
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


def render_report(report: Report) -> None:
    """Terminal rendering of a Report, reusing the same object md/json use (spec §6.1).
    `info`-level findings are intentionally omitted here; they appear only in md/json."""
    console.print(f"[bold]{report.source or '?'} → {report.target or '?'}[/bold]")
    cov = report.coverage
    console.print(
        f"{cov.get('found', 0)} found, {cov.get('translated', 0)} translated, "
        f"{cov.get('decided', 0)} decided, {cov.get('needs_action', 0)} need action, "
        f"{cov.get('not_supported', 0)} not yet supported.",
        markup=False,
    )
    if report.scan_gaps:
        console.print(f"[bold]Scan gaps ({len(report.scan_gaps)})[/bold]")
        for f in report.scan_gaps:
            console.print(f"  {f.code}: {f.message}", markup=False)
    if report.needs_action:
        console.print(f"[bold]Needs your action ({len(report.needs_action)})[/bold]")
        for item in report.needs_action:
            console.print(f"  {item}", markup=False)
    if report.not_supported:
        console.print(f"[bold]Not yet supported ({len(report.not_supported)})[/bold]")
        for f in report.not_supported:
            console.print(f"  {f.code}: {f.message}", markup=False)
    if report.plan is not None:
        console.print(
            f"Plan: +{report.plan.add} ~{report.plan.change} -{report.plan.destroy}",
            markup=False,
        )
        for addr in report.plan.changed_or_destroyed:
            console.print(f"  [red]{addr}[/red]")


def _write_and_note_report(project: Project) -> None:
    report = build_report(project)
    write_report(project, report)
    console.print(f"Report: {project.report_md_path.relative_to(project.root)}", markup=False)


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
def analyze(
    vpc: Annotated[
        list[str] | None,
        typer.Option("--vpc", help="Only migrate resources in this VPC (id or name); repeatable."),
    ] = None,
    tag: Annotated[
        list[str] | None,
        typer.Option(
            "--tag", help="Only migrate resources tagged KEY or KEY=VALUE; repeatable (any match)."
        ),
    ] = None,
    exclude: Annotated[
        list[str] | None,
        typer.Option("--exclude", help="Leave this resource out (id or name); repeatable."),
    ] = None,
    clear_scope: bool = typer.Option(
        False, "--clear-scope", help="Forget the saved scope and migrate everything scanned."
    ),
) -> None:
    """Turn the latest scan into .easymigrate/ir.json.

    The scope (--vpc, --tag, --exclude) is saved with the project, so re-running analyze keeps it.
    """
    project = Project(Path.cwd())
    with _user_errors():
        if vpc or tag or exclude or clear_scope:
            scope = set_scope(project, vpcs=vpc, tags=tag, exclude=exclude, clear=clear_scope)
        else:
            scope = project.load_config().scope if project.config_path.exists() else None
        doc = run_analyze(project)
    if scope is not None:
        parts = [
            f"{label} {', '.join(values)}"
            for label, values in (
                ("VPC", scope.vpcs),
                ("tag", scope.tags),
                ("excluding", scope.exclude),
            )
            if values
        ]
        console.print(f"Scope: {'; '.join(parts)} (saved; --clear-scope to remove)", markup=False)
        for finding in doc.findings:
            if finding.code.startswith("SCOPE-"):
                console.print(f"  {finding.code}: {finding.message}", markup=False)
    counts = Counter(obj.kind for obj in doc.objects)
    table = Table("Kind", "Count", title="Intent objects")
    for kind in sorted(counts):
        table.add_row(kind, str(counts[kind]))
    console.print(table)
    _print_findings(doc.findings)
    console.print(f"Wrote {project.ir_path.relative_to(project.root)}.", markup=False)
    _write_and_note_report(project)


def _print_params(rule: Any, diff: Any, current: str, chosen_id: str, current_params: dict) -> None:
    params = current_params if chosen_id == current else rule.default_params(diff, chosen_id)
    if not params:
        return
    for key, value in params.items():
        console.print(f"  {key}: {value}", markup=False)
    console.print("  Edit decisions.yaml to change these.", markup=False)


def _prompt_choice(view: dict[str, Any]) -> tuple[str, bool]:
    diff = view["difference"]
    rule = view["rule"]
    current = view["current_strategy"]
    current_acknowledged = view.get("current_acknowledged", False)
    current_params = view.get("current_params", {})

    allowed = rule.allowed_strategies(diff)
    default_strategy = rule.default_strategy(diff)

    console.print(f"[bold]{rule.title}[/bold] ({diff.key})")
    console.print(f"  subjects: {', '.join(diff.subjects)}", markup=False)
    console.print(f"  impact: {rule.impact}", markup=False)
    if diff.context:
        console.print(f"  context: {diff.context}", markup=False)
    for index, strategy in enumerate(allowed, start=1):
        marker = " (default)" if strategy.id == default_strategy else ""
        risk = rule.risk_for(diff, strategy.id)
        console.print(
            f"  {index}. {strategy.id} — {strategy.effect} (risk: {risk}){marker}",
            markup=False,
        )

    while True:
        raw = typer.prompt("Strategy (Enter to keep current)", default="", show_default=False)
        if not raw.strip():
            chosen = rule.strategy(current)
            if chosen.manual and current_acknowledged:
                # Keeping an already-acknowledged manual strategy needs no re-confirmation.
                _print_params(rule, diff, current, chosen.id, current_params)
                return chosen.id, True
        else:
            try:
                index = int(raw.strip()) - 1
                if not 0 <= index < len(allowed):
                    raise ValueError
            except ValueError:
                console.print("Invalid choice.", markup=False)
                continue
            chosen = allowed[index]
        if not chosen.manual:
            _print_params(rule, diff, current, chosen.id, current_params)
            return chosen.id, False
        answer = typer.prompt("I will handle this myself [y/N]", default="n", show_default=False)
        if answer.strip().lower() == "y":
            _print_params(rule, diff, current, chosen.id, current_params)
            return chosen.id, True
        # anything else re-asks the strategy choice


@app.command()
def resolve(
    accept_defaults: bool = typer.Option(
        False, "--accept-defaults", help="Choose every default without prompting."
    ),
    acknowledge_manual: bool = typer.Option(
        False,
        "--acknowledge-manual",
        help="Acknowledge every manual decision (with --accept-defaults).",
    ),
) -> None:
    """Choose a strategy for every AWS→GCP difference and save decisions.yaml."""
    if acknowledge_manual and not accept_defaults:
        _fail("--acknowledge-manual requires --accept-defaults")
    project = Project(Path.cwd())
    choose = None if accept_defaults else _prompt_choice
    with _user_errors():
        summary = run_resolve(
            project,
            choose=choose,
            accept_defaults=accept_defaults,
            acknowledge_manual=acknowledge_manual,
        )
    if summary.dropped:
        console.print(f"Dropped {len(summary.dropped)} decision(s) no longer needed:", markup=False)
        for key in summary.dropped:
            console.print(f"  {key}", markup=False)
    table = Table("Key", "Strategy", "Decided by", "Acknowledged", title="Decisions")
    for decision in summary.decisions.decisions:
        table.add_row(
            decision.key, decision.strategy, decision.decided_by, str(decision.acknowledged)
        )
    console.print(table)
    if summary.superseded:
        console.print(
            f"[yellow]Decisions that no longer apply ({len(summary.superseded)})[/yellow]"
        )
        for entry in summary.superseded:
            console.print(f"  {entry.message}", markup=False)
            console.print(
                f"  choose a different strategy for {entry.superseded_by} to keep this decision",
                markup=False,
            )
    _write_and_note_report(project)
    if summary.problems:
        for problem in summary.problems:
            error_console.print(f"[red]{problem}[/red]")
        raise typer.Exit(code=2)
    console.print("Next: run `easymigrate plan`.", markup=False)


def _print_plan_summary(summary: PlanSummary) -> None:
    table = Table("Add", "Change", "Destroy", title="Plan")
    table.add_row(str(summary.add), str(summary.change), str(summary.destroy))
    console.print(table)
    _print_findings(summary.findings)
    if summary.changed_or_destroyed:
        console.print("[bold]Changed or destroyed[/bold]")
        for address in summary.changed_or_destroyed:
            console.print(f"  [red]{address}[/red]")
    if summary.superseded:
        console.print(
            f"[yellow]Decisions that no longer apply ({len(summary.superseded)})[/yellow]"
        )
        for entry in summary.superseded:
            console.print(f"  {entry.message}", markup=False)


def _parse_db_passwords(values: list[str]) -> dict[str, str]:
    """Parse repeated `--db-password <ir-id>=<value>` options. The id is validated against
    the resolved IR inside `plan`, which is the only place that knows it."""
    passwords: dict[str, str] = {}
    for value in values:
        ir_id, separator, secret = value.partition("=")
        if not separator or not ir_id:
            _fail(
                "--db-password takes <ir-id>=<value>, for example "
                "--db-password database_instance:prod-db=s3cret"
            )
        passwords[ir_id] = secret
    return passwords


def _stdin_is_tty() -> bool:
    """A seam: `plan` only offers an interactive password prompt on a terminal, and tests
    drive both sides of that without replacing sys.stdin."""
    return sys.stdin.isatty()


def _ask_db_password(ir_id: str) -> str:
    return typer.prompt(f"Password for {ir_id}", hide_input=True)


@app.command()
def plan(
    gcp_project: str = typer.Option(..., "--gcp-project", help="Destination GCP project id."),
    gcp_region: str | None = typer.Option(
        None, "--gcp-region", help="GCP region; inferred from the scanned AWS region if omitted."
    ),
    db_password: Annotated[
        list[str] | None,
        typer.Option(
            "--db-password",
            metavar="ID=VALUE",
            help=(
                "Password for a migrated database, as <ir-id>=<value>; repeatable. "
                "EasyMigrate never invents one: AWS does not return the original."
            ),
        ),
    ] = None,
    create_state_bucket: bool = typer.Option(
        False,
        "--create-state-bucket",
        help="Create the Terraform state bucket without asking if it does not exist.",
    ),
) -> None:
    """Generate GCP Terraform from the resolved decisions and run `tofu plan`."""
    project = Project(Path.cwd())
    with _user_errors():
        passwords = _parse_db_passwords(db_password or [])
        config = project.load_config()
        if config.source is None:
            _fail("no scan found; run `easymigrate scan` first")
        region = gcp_region_for(config.source.region, override=gcp_region)

        def confirm_bucket(name: str) -> bool:
            if create_state_bucket:
                return True
            if not _stdin_is_tty():
                # Prompting with no terminal blocks forever on a stdin nothing will write to
                # (live run, 2026-09-22), so decline and let the error name the flag.
                return False
            return typer.confirm(
                f"State bucket {name} does not exist. Create it in {region}? [y/N]",
                default=False,
            )

        summary = run_plan(
            project,
            gcp_project=gcp_project,
            gcp_region=gcp_region,
            confirm_bucket=confirm_bucket,
            db_passwords=passwords,
            ask_password=_ask_db_password if _stdin_is_tty() else None,
        )
    _print_plan_summary(summary)
    console.print(f"Plan: {summary.plan_text_path.relative_to(project.root)}", markup=False)
    console.print("Next: run `easymigrate apply`.", markup=False)


@app.command()
def apply(
    yes: bool = typer.Option(
        False, "--yes", help="Apply without asking; required when not run from a terminal."
    ),
) -> None:
    """Apply exactly the plan `easymigrate plan` produced, after confirmation."""
    project = Project(Path.cwd())
    with _user_errors():
        config = project.load_config()
        target_project = config.target.project if config.target else "?"

        def confirm(summary: PlanSummary) -> bool:
            _print_plan_summary(summary)
            if yes:
                return True
            if not _stdin_is_tty():
                _fail("not a terminal, so apply cannot ask for confirmation; re-run with --yes")
            return typer.confirm(
                f"Apply these changes to project {target_project}? [y/N]", default=False
            )

        def stream(line: str) -> None:
            console.print(line, markup=False)

        result = run_apply(project, confirm=confirm, stream=stream)
    console.print("Apply complete.", markup=False)
    _write_and_note_report(project)
    if result.summary.superseded:
        console.print(
            f"[yellow]Decisions that no longer applied this run "
            f"({len(result.summary.superseded)})[/yellow]"
        )


@app.command()
def destroy(
    yes: bool = typer.Option(
        False, "--yes", help="Destroy without asking; required when not run from a terminal."
    ),
    remove_protection: bool = typer.Option(
        False,
        "--remove-protection",
        help="Turn off deletion protection carried over from AWS and destroy those resources too.",
    ),
    delete_state_bucket: bool = typer.Option(
        False,
        "--delete-state-bucket",
        help="Also delete the gs://<project>-easymigrate-tfstate bucket afterwards.",
    ),
) -> None:
    """Undo `apply`: delete everything EasyMigrate created on GCP. AWS is never touched."""
    project = Project(Path.cwd())
    with _user_errors():

        def confirm(summary: DestroySummary) -> bool:
            console.print(
                f"[bold]{len(summary.addresses)} resource(s) will be deleted from "
                f"{summary.gcp_project}[/bold]"
            )
            for address in summary.addresses:
                marker = (
                    "  (deletion protection will be turned off)"
                    if (address in summary.protected)
                    else ""
                )
                console.print(f"  - {address}{marker}", markup=False)
            if yes:
                return True
            if not _stdin_is_tty():
                _fail("not a terminal, so destroy cannot ask for confirmation; re-run with --yes")
            return typer.confirm(
                f"Delete these from {summary.gcp_project}? This cannot be undone. [y/N]",
                default=False,
            )

        def stream(line: str) -> None:
            console.print(line, markup=False)

        result = run_destroy(
            project,
            confirm=confirm,
            stream=stream,
            remove_protection=remove_protection,
            delete_state_bucket=delete_state_bucket,
        )
    console.print(
        f"Destroy complete: {len(result.summary.addresses)} resource(s) deleted.", markup=False
    )
    if delete_state_bucket and not result.bucket_deleted:
        console.print("The state bucket was already gone.", markup=False)


def _serve(fastapi_app: Any, sock: socket.socket) -> None:  # pragma: no cover - real server loop
    import uvicorn

    config = uvicorn.Config(fastapi_app, log_level="warning")
    uvicorn.Server(config).run(sockets=[sock])


def _bind_loopback_socket(port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", port))
    except OSError:
        sock.close()
        _fail(f"port {port} is already in use" if port else "could not bind a free port")
    sock.listen()
    return sock


@app.command()
def ui(
    port: int = typer.Option(0, "--port", help="Port to bind; 0 (default) picks a free port."),
    no_browser: bool = typer.Option(
        False, "--no-browser", help="Don't open the browser automatically."
    ),
) -> None:
    """Start the local review UI on 127.0.0.1 (never a network-exposed host)."""
    try:
        from easymigrate.ui import create_app
        from easymigrate.ui.security import Security, new_token
    except ImportError:
        _fail(r'the ui extra is not installed; run `uv tool install "easymigrate\[ui]"`')

    project = Project(Path.cwd())
    sock = _bind_loopback_socket(port)
    bound_port = sock.getsockname()[1]

    token = new_token()
    security = Security(token=token, host="127.0.0.1", port=bound_port)
    fastapi_app = create_app(project, security)

    url = f"http://127.0.0.1:{bound_port}/?token={token}"
    console.print(f"EasyMigrate UI: {url}", markup=False)
    if not no_browser:
        webbrowser.open(url)

    _serve(fastapi_app, sock)
