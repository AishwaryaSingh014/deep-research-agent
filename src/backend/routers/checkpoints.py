"""Recovering finished reports from the checkpoint database.

A run's research survives the process that produced it. These endpoints read a completed
thread's report back out of ``cache/checkpoints.db`` — the difference between "the server
restarted, your report is gone" and "here it is".
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from deepresearch import graph
from deepresearch import report as report_module

router = APIRouter(prefix="/checkpoints", tags=["checkpoints"])


def _finished_or_404(thread_id: str) -> tuple[str, str]:
    found = graph.checkpoint_report(thread_id)
    if found is None:
        raise HTTPException(
            status_code=404,
            detail=f"No finished report for {thread_id!r} — unknown thread, or still mid-pipeline.",
        )
    return found


@router.get("/{thread_id}/report")
def get_checkpoint_report(thread_id: str) -> dict:
    """Recover the finished report of a completed run, even from a previous process.

    Runs submitted through the API used to keep their markdown only in memory, so a restart
    lost the report while leaving all the research sitting in the checkpoint. This reads it
    back out.
    """
    question, markdown = _finished_or_404(thread_id)
    return {"thread_id": thread_id, "question": question, "markdown": markdown}


@router.post("/{thread_id}/save")
def save_checkpoint_report(thread_id: str) -> dict:
    """Write a completed checkpoint's report to outputs/."""
    question, markdown = _finished_or_404(thread_id)
    try:
        path = report_module.save(question, markdown)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not write report: {exc}") from exc
    return {
        "thread_id": thread_id,
        "question": question,
        "path": str(path),
        "filename": path.name,
        "chars": len(markdown),
    }
