"""Project state, derived entirely from files on disk (spec §2, §5).

The filesystem is the state: every call re-reads `.easymigrate/` from scratch so the
CLI and UI stay interchangeable on one project. Nothing here may be cached in server
memory — a project with no `.easymigrate/` yet simply reports every stage incomplete
rather than raising.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from easymigrate.core.pipeline import load_differences
from easymigrate.core.plugin import SCAN_FILE
from easymigrate.core.project import Project
from easymigrate.differences.decisions import from_yaml, readiness_problems
from easymigrate.differences.engine import catalogue
from easymigrate.ir.base import StrictModel


class StageState(StrictModel):
    name: str
    complete: bool
    artifact_at: datetime | None = None


class ProjectState(StrictModel):
    root: str
    stages: list[StageState]
    source: dict | None = None
    target: dict | None = None
    blocked: list[str] = []


def project_state(project: Project) -> ProjectState:
    stages = [
        _stage_from_file("scan", project.snapshot_dir("aws") / SCAN_FILE),
        _stage_from_file("analyze", project.ir_path),
        _stage_from_file("resolve", project.decisions_path),
        _stage_from_file("plan", project.tfplan_path),
        _apply_stage(project),
    ]
    source, target = _source_target(project)
    return ProjectState(
        root=str(project.root),
        stages=stages,
        source=source,
        target=target,
        blocked=_blocked(project),
    )


def _stage_from_file(name: str, path: Path) -> StageState:
    if not path.exists():
        return StageState(name=name, complete=False, artifact_at=None)
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    return StageState(name=name, complete=True, artifact_at=mtime)


def _apply_stage(project: Project) -> StageState:
    """Forward-compatible with plan 2's `.easymigrate/jobs/<id>.json` records (spec §4):
    complete when a `stage == "apply"`, `status == "succeeded"` record exists. Plan 1
    writes no jobs itself, so this stage stays incomplete until plan 2 lands."""
    jobs_dir = project.dir / "jobs"
    latest: datetime | None = None
    if jobs_dir.is_dir():
        for job_file in sorted(jobs_dir.glob("*.json")):
            try:
                data = json.loads(job_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if data.get("stage") != "apply" or data.get("status") != "succeeded":
                continue
            finished_at = data.get("finished_at")
            timestamp = (
                datetime.fromisoformat(finished_at)
                if finished_at
                else datetime.fromtimestamp(job_file.stat().st_mtime, tz=UTC)
            )
            if latest is None or timestamp > latest:
                latest = timestamp
    return StageState(name="apply", complete=latest is not None, artifact_at=latest)


def _source_target(project: Project) -> tuple[dict | None, dict | None]:
    if not project.config_path.exists():
        return None, None
    config = project.load_config()
    source = config.source.model_dump(mode="json") if config.source else None
    target = config.target.model_dump(mode="json") if config.target else None
    return source, target


def _blocked(project: Project) -> list[str]:
    if not project.differences_path.exists():
        return []
    differences = load_differences(project)
    decisions = (
        from_yaml(project.decisions_path.read_text(encoding="utf-8"))
        if project.decisions_path.exists()
        else None
    )
    return readiness_problems(decisions, differences, catalogue())
