"""Submitting, inspecting, resuming and streaming research runs.

The pipeline is synchronous and takes minutes, so no endpoint here runs it inline. Submitting
a question enqueues a job and returns 202 immediately; progress is streamed over SSE and the
finished report is fetched separately.
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from deepresearch import config, graph

from ..schemas import ResearchRequest
from ..services.jobs import MANAGER, SENTINEL

# How long an idle SSE connection waits before emitting a keep-alive comment. Without this,
# proxies and browsers quietly drop a stream that goes silent during a slow node.
HEARTBEAT_SECONDS = 15.0

router = APIRouter(tags=["research"])


@router.post("/research", status_code=202)
def start_research(req: ResearchRequest) -> dict:
    """Enqueue a research run. Returns immediately — poll /research/{id} or stream /events."""
    if not (config.GROQ_API_KEY or config.GEMINI_API_KEY):
        raise HTTPException(
            status_code=503,
            detail="No LLM provider configured. Set GROQ_API_KEY or GEMINI_API_KEY in .env.",
        )

    job = MANAGER.submit(req.question, fresh=req.fresh)
    return {
        "run_id": job.run_id,
        "status": job.status,
        "position": MANAGER.position(job.run_id),
        "events_url": f"/research/{job.run_id}/events",
    }


@router.get("/research/{run_id}")
def get_research(run_id: str) -> dict:
    job = MANAGER.get(run_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"No run {run_id}")
    payload = job.to_dict()
    payload["position"] = MANAGER.position(run_id)
    return payload


@router.post("/research/{run_id}/resume", status_code=202)
def resume_research(run_id: str) -> dict:
    """Continue a failed run from its last completed node.

    Provider exhaustion aborts a run mid-pipeline, but the completed nodes are checkpointed.
    Re-submitting the same question picks up from there — typically finishing in seconds
    rather than repeating several minutes of search and reading.
    """
    job = MANAGER.get(run_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"No run {run_id}")
    if job.status not in ("failed",):
        raise HTTPException(
            status_code=409,
            detail=f"Run is {job.status}, not failed — nothing to resume.",
        )

    # fresh=False is the whole point: the thread id is derived from the question, so the
    # graph picks the checkpoint back up.
    new_job = MANAGER.submit(job.question, fresh=False)
    return {
        "run_id": new_job.run_id,
        "resumed_from": run_id,
        "status": new_job.status,
        "position": MANAGER.position(new_job.run_id),
        "events_url": f"/research/{new_job.run_id}/events",
    }


@router.get("/research/{run_id}/events")
async def stream_events(run_id: str) -> StreamingResponse:
    """Server-Sent Events: one event per agent action, then a terminal done/error event."""
    job = MANAGER.get(run_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"No run {run_id}")

    subscriber = job.subscribe()

    async def event_stream():
        try:
            while True:
                try:
                    # get() is blocking, so it goes to a thread to keep the loop responsive.
                    event = await asyncio.wait_for(
                        asyncio.to_thread(subscriber.get, True, HEARTBEAT_SECONDS),
                        timeout=HEARTBEAT_SECONDS + 5,
                    )
                except (TimeoutError, asyncio.TimeoutError):
                    # Comment frame: keeps proxies from closing a stream during a slow node.
                    yield ": keep-alive\n\n"
                    continue

                if event is SENTINEL:
                    break
                yield f"data: {json.dumps(event)}\n\n"
        finally:
            job.unsubscribe(subscriber)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # stops nginx buffering the stream into uselessness
        },
    )


@router.get("/runs")
def list_runs() -> dict:
    """In-flight and completed runs from this process, plus every checkpoint on disk.

    Checkpoints are described by what the graph would do next, not by how many rows they
    have. The row count is identical for a finished run and one that died at the first node,
    so reporting it invites exactly the wrong conclusion.
    """
    jobs = [job.to_dict(include_report=False) for job in MANAGER.list()]
    checkpoints = graph.checkpoint_summaries()
    return {
        "runs": jobs,
        "checkpoints": checkpoints,
        "resumable": [c for c in checkpoints if not c["complete"]],
        "busy": MANAGER.current_run_id is not None,
    }
