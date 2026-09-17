"""GET /api/jobs/{id}/stream: replay-from-byte-0 SSE log stream (spec §4, §5, plan 2
task 4).

The end-to-end tests drive the real route through `TestClient`; the mid-job and
race-condition tests drive `_events` (the generator behind the route) directly with
`asyncio`, since the in-process ASGI test transport buffers a streaming response until
it closes and cannot demonstrate genuine interleaving on its own.
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from easymigrate.core.project import Project
from easymigrate.ui.app import create_app
from easymigrate.ui.jobs import JobRecord, JobStore
from easymigrate.ui.routes.jobs import _events
from easymigrate.ui.security import Security

PORT = 54766
TOKEN = "stream-test-token"
HEADERS = {"Host": f"127.0.0.1:{PORT}"}


@pytest.fixture
def project(tmp_path: Path) -> Project:
    return Project(tmp_path)


@pytest.fixture
def security() -> Security:
    return Security(token=TOKEN, host="127.0.0.1", port=PORT)


@pytest.fixture
def client(project: Project, security: Security) -> TestClient:
    return TestClient(create_app(project, security))


def _parse_sse(text: str) -> list[tuple[str, str]]:
    events: list[tuple[str, str]] = []
    event_name = "message"
    data_lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("event:"):
            event_name = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:") :].strip())
        elif line == "":
            if data_lines:
                events.append((event_name, "\n".join(data_lines)))
            event_name = "message"
            data_lines = []
    if data_lines:
        events.append((event_name, "\n".join(data_lines)))
    return events


def _wait_terminal(store: JobStore, job_id: str, timeout: float = 5.0) -> JobRecord:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = store.get(job_id)
        if record.status != "running":
            return record
        time.sleep(0.01)
    raise AssertionError("job did not reach a terminal state in time")


# --- end-to-end, via the real route -------------------------------------------------


def test_client_attaching_after_completion_gets_the_whole_log_then_terminal(
    client: TestClient, project: Project
):
    store = JobStore(project)

    def work(stream):
        stream("line one")
        stream("line two")

    record = store.start("scan", work)
    _wait_terminal(store, record.id)

    resp = client.get(f"/api/jobs/{record.id}/stream", headers=HEADERS)
    assert resp.status_code == 200
    events = _parse_sse(resp.text)

    log_lines = [data for name, data in events if name == "log"]
    assert log_lines == ["line one", "line two"]
    terminal = [data for name, data in events if name == "terminal"]
    assert len(terminal) == 1
    assert '"status":"succeeded"' in terminal[0]


def test_stream_of_unknown_job_is_404(client: TestClient):
    resp = client.get("/api/jobs/does-not-exist/stream", headers=HEADERS)
    assert resp.status_code == 404


def test_stream_of_failed_job_carries_failed_status_in_terminal_event(
    client: TestClient, project: Project
):
    store = JobStore(project)

    def work(stream):
        raise ValueError("boom")

    record = store.start("plan", work)
    _wait_terminal(store, record.id)

    resp = client.get(f"/api/jobs/{record.id}/stream", headers=HEADERS)
    events = _parse_sse(resp.text)
    terminal = [data for name, data in events if name == "terminal"]
    assert len(terminal) == 1
    assert '"status":"failed"' in terminal[0]
    assert "boom" in terminal[0]


# --- the generator itself, driven directly for genuine interleaving -----------------


def test_generator_attached_mid_job_yields_earlier_then_later_lines(project: Project):
    store = JobStore(project)
    gate = threading.Event()

    def work(stream):
        stream("first line")
        gate.wait(5)
        stream("second line")

    record = store.start("scan", work)
    _wait_for_content(store.log_path(record.id), "first line")

    async def run():
        gen = _events(store, record.id)
        first = await gen.__anext__()
        assert first == {"event": "log", "data": "first line"}
        # Release the job only after we've genuinely observed the earlier line first.
        gate.set()
        second = await gen.__anext__()
        assert second == {"event": "log", "data": "second line"}
        terminal = await gen.__anext__()
        assert terminal["event"] == "terminal"
        assert '"status":"succeeded"' in terminal["data"]
        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()

    asyncio.run(run())


def test_generator_does_not_lose_a_line_written_between_the_read_and_the_status_check(
    project: Project, tmp_path: Path
):
    """The exact race the plan calls out: a line lands on disk in the same instant the
    record flips to terminal. Without the second read after observing a terminal
    status, that line would never be read again."""
    log_path = tmp_path / "race.log"
    log_path.write_text("line one\n", encoding="utf-8")

    class RaceStore:
        def __init__(self) -> None:
            self.calls = 0

        def log_path(self, job_id: str) -> Path:
            return log_path

        def get(self, job_id: str) -> JobRecord:
            self.calls += 1
            now = datetime.now(UTC)
            if self.calls == 1:
                return JobRecord(id=job_id, stage="scan", status="running", started_at=now, pid=1)
            # Simulate the writer landing its last line in the same instant it flips
            # the record to a terminal status.
            with open(log_path, "a", encoding="utf-8") as handle:
                handle.write("line two\n")
            return JobRecord(
                id=job_id,
                stage="scan",
                status="succeeded",
                started_at=now,
                finished_at=now,
                pid=1,
            )

    async def run():
        events = [event async for event in _events(RaceStore(), "job-1")]
        return events

    events = asyncio.run(run())
    log_events = [e["data"] for e in events if e["event"] == "log"]
    assert log_events == ["line one", "line two"]
    assert events[-1]["event"] == "terminal"


def _wait_for_content(path: Path, needle: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and needle in path.read_text(encoding="utf-8"):
            return
        time.sleep(0.01)
    raise AssertionError(f"{needle!r} never appeared in {path}")
