"""Background jobs for `scan`, `plan` and `apply` (spec §4).

Job state lives on disk, not in memory: a `JobStore` never caches a record. The record
file (`.easymigrate/jobs/<id>.json`) is the only truth a reader may trust, which is what
lets a closed browser tab reconnect to a job in progress and what lets `reap_stale`
detect a job the server died mid-run.

Every pipeline callback a job's `work` calls into is pre-satisfied before the job starts
(spec §3) — a job never waits on a human.
"""

from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from easymigrate.core.project import Project
from easymigrate.ir.base import StrictModel

Status = Literal["running", "succeeded", "failed"]

_SERVER_STOPPED_ERROR = (
    "the EasyMigrate server stopped while this {stage} was running; its result is unknown"
)


class JobAlreadyRunning(Exception):
    """Raised by `JobStore.start` when a job is already active for this project."""


class JobRecord(StrictModel):
    id: str
    stage: str
    status: Status
    started_at: datetime
    finished_at: datetime | None = None
    error: str | None = None
    pid: int


class JobStore:
    """One job at a time per project. `start` writes the record before the work begins,
    so a crash between writing and launching can never orphan a running thread with no
    record on disk."""

    def __init__(self, project: Project) -> None:
        self.project = project
        self._lock = threading.Lock()

    def _record_path(self, job_id: str) -> Path:
        return self.project.jobs_dir / f"{job_id}.json"

    def log_path(self, job_id: str) -> Path:
        return self.project.jobs_dir / f"{job_id}.log"

    def _write(self, record: JobRecord) -> None:
        self.project.write_json(self._record_path(record.id), record)

    def _all_records(self) -> list[JobRecord]:
        jobs_dir = self.project.jobs_dir
        if not jobs_dir.is_dir():
            return []
        records = []
        for path in jobs_dir.glob("*.json"):
            try:
                records.append(JobRecord.model_validate_json(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return records

    def active(self) -> JobRecord | None:
        running = [record for record in self._all_records() if record.status == "running"]
        if not running:
            return None
        running.sort(key=lambda record: record.started_at)
        return running[-1]

    def get(self, job_id: str) -> JobRecord:
        path = self._record_path(job_id)
        if not path.exists():
            raise KeyError(job_id)
        return JobRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def start(self, stage: str, work: Callable[[Callable[[str], None]], None]) -> JobRecord:
        with self._lock:
            if self.active() is not None:
                raise JobAlreadyRunning(f"a job is already running for {self.project.root}")
            record = JobRecord(
                id=uuid.uuid4().hex,
                stage=stage,
                status="running",
                started_at=datetime.now(UTC),
                pid=os.getpid(),
            )
            self.project.write_text(self.log_path(record.id), "")
            self._write(record)

        def stream(line: str) -> None:
            with open(self.log_path(record.id), "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())

        def run() -> None:
            try:
                work(stream)
            except Exception as exc:  # noqa: BLE001 - the engine's own error, verbatim
                self._write(
                    record.model_copy(
                        update={
                            "status": "failed",
                            "finished_at": datetime.now(UTC),
                            "error": str(exc),
                        }
                    )
                )
            else:
                self._write(
                    record.model_copy(
                        update={"status": "succeeded", "finished_at": datetime.now(UTC)}
                    )
                )

        threading.Thread(target=run, daemon=True).start()
        return record

    def reap_stale(self) -> list[JobRecord]:
        """Called once at server startup (spec §4): any record left `running` by a
        process that is no longer this one is dead, not live. Showing it as live would be
        a lie about the state of someone's cloud."""
        reaped = []
        current_pid = os.getpid()
        for record in self._all_records():
            if record.status != "running" or record.pid == current_pid:
                continue
            updated = record.model_copy(
                update={
                    "status": "failed",
                    "finished_at": datetime.now(UTC),
                    "error": _SERVER_STOPPED_ERROR.format(stage=record.stage),
                }
            )
            self._write(updated)
            reaped.append(updated)
        return reaped
