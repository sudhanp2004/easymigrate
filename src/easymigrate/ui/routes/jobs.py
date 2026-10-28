"""`GET /api/jobs/{id}` and `GET /api/jobs/{id}/stream`: job status and its
Server-Sent Events log stream (spec §4, §5).

The stream replays the log from byte 0 before tailing, so a user who opens the page
late still sees the whole run, and it does not end until the record has reached a
terminal status **and** the log has been fully read — reading the log first and only
then checking the record on each poll is what keeps the last lines written just before
completion from being lost to a race.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from fastapi import Depends, FastAPI, HTTPException
from sse_starlette.sse import EventSourceResponse

from easymigrate.ui.jobs import JobStore
from easymigrate.ui.routes.deps import RouteDeps

_POLL_INTERVAL = 0.02


def register(app: FastAPI, deps: RouteDeps) -> None:
    store = Depends(deps.jobs)

    @app.get("/api/jobs/{job_id}", dependencies=deps.reads)
    def get_job(job_id: str, jobs: JobStore = store):
        try:
            return jobs.get(job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="job not found") from None

    @app.get("/api/jobs/{job_id}/stream", dependencies=deps.reads)
    async def stream_job(job_id: str, jobs: JobStore = store) -> EventSourceResponse:
        try:
            jobs.get(job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="job not found") from None

        return EventSourceResponse(_events(jobs, job_id))


async def _events(store: JobStore, job_id: str) -> AsyncIterator[dict]:
    """The generator behind `GET /api/jobs/{id}/stream`, kept at module scope so it can
    be driven directly in tests without going through a real HTTP connection."""
    log_path = store.log_path(job_id)
    position = 0
    while True:
        position, lines = _read_new_lines(log_path, position)
        for line in lines:
            yield {"event": "log", "data": line}

        record = store.get(job_id)
        if record.status != "running":
            # The writer only flips the record to a terminal status after every log
            # line has been written, but a line written between our read above and
            # this check would otherwise be lost — read once more before closing.
            position, lines = _read_new_lines(log_path, position)
            for line in lines:
                yield {"event": "log", "data": line}
            yield {"event": "terminal", "data": record.model_dump_json()}
            return

        await asyncio.sleep(_POLL_INTERVAL)


def _read_new_lines(log_path, position: int) -> tuple[int, list[str]]:
    if not log_path.exists():
        return position, []
    with open(log_path, encoding="utf-8") as handle:
        handle.seek(position)
        chunk = handle.read()
        new_position = handle.tell()
    if not chunk:
        return position, []
    lines = chunk.splitlines()
    return new_position, lines
