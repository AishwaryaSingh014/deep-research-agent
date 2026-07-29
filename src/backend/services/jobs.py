"""Single-worker job queue for research runs.

Why serialised rather than concurrent: the pipeline keeps its progress callback, token ledger,
search budget and rate-limiter state in module globals, so two runs in one process would
interleave and corrupt each other's output. That constraint is not the real reason though —
the real reason is that free-tier providers cannot sustain two concurrent research runs. Two
parallel jobs would simply rate-limit each other into failure.

So the queue is honest about it: one run at a time, and callers are told their position rather
than being quietly starved. Making runs genuinely parallel means threading a per-run context
through every module *and* paid API keys; that belongs on the roadmap, not here.
"""

from __future__ import annotations

import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from deepresearch import graph, llm, report

Status = Literal["queued", "running", "done", "failed"]

# Sent to a run's event stream when it reaches a terminal state. A stream that merely stops
# is indistinguishable from a hung backend, so every run ends with an explicit event.
SENTINEL = object()


@dataclass
class Job:
    run_id: str
    question: str
    fresh: bool = False
    status: Status = "queued"
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    events: list[dict] = field(default_factory=list)
    markdown: str = ""
    stats: dict = field(default_factory=dict)
    approved: bool = False
    resumed: bool = False
    error: str | None = None
    # Where the report was written. Set on success; None if the write itself failed.
    report_path: str | None = None
    # Live subscribers (SSE connections). A run may have zero, one, or several.
    _subscribers: list[queue.Queue] = field(default_factory=list, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def publish(self, event: dict) -> None:
        with self._lock:
            self.events.append(event)
            subscribers = list(self._subscribers)
        for sub in subscribers:
            sub.put(event)

    def subscribe(self) -> queue.Queue:
        """Attach a listener, replaying history so a late client sees the whole run."""
        q: queue.Queue = queue.Queue()
        with self._lock:
            for event in self.events:
                q.put(event)
            if self.status in ("done", "failed"):
                q.put(SENTINEL)
            else:
                self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def close(self) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
            self._subscribers.clear()
        for sub in subscribers:
            sub.put(SENTINEL)

    @property
    def resumable(self) -> bool:
        """A failed run is not lost work — its completed nodes are on disk.

        A single unrecoverable provider error aborts the run, but everything up to that node
        is checkpointed, so re-submitting the same question continues from there rather than
        starting over. Surfacing this is the difference between "your 5 minutes are gone" and
        "press resume".
        """
        return self.status == "failed"

    def to_dict(self, include_report: bool = True) -> dict[str, Any]:
        payload = {
            "run_id": self.run_id,
            "question": self.question,
            "status": self.status,
            "resumable": self.resumable,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_s": (
                (self.finished_at or time.time()) - self.started_at if self.started_at else 0.0
            ),
            "approved": self.approved,
            "resumed": self.resumed,
            "event_count": len(self.events),
            "error": self.error,
            "report_path": self.report_path,
        }
        if include_report:
            payload["markdown"] = self.markdown
            payload["stats"] = self.stats
        return payload


class JobManager:
    """Owns the queue, the worker thread, and the run registry."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._pending: queue.Queue[str] = queue.Queue()
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        self.current_run_id: str | None = None

    # ----------------------------------------------------------------- #
    def start(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker = threading.Thread(target=self._loop, name="research-worker", daemon=True)
        self._worker.start()

    def stop(self) -> None:
        self._stop.set()

    # ----------------------------------------------------------------- #
    def submit(self, question: str, fresh: bool = False) -> Job:
        job = Job(run_id=uuid.uuid4().hex[:12], question=question.strip(), fresh=fresh)
        with self._lock:
            self._jobs[job.run_id] = job
        self._pending.put(job.run_id)
        return job

    def get(self, run_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(run_id)

    def list(self) -> list[Job]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)

    def position(self, run_id: str) -> int:
        """0 = running or next up; N = N jobs ahead of it."""
        job = self.get(run_id)
        if not job or job.status != "queued":
            return 0
        ahead = [
            j
            for j in self.list()
            if j.status == "queued" and j.created_at < job.created_at
        ]
        return len(ahead) + (1 if self.current_run_id else 0)

    # ----------------------------------------------------------------- #
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                run_id = self._pending.get(timeout=0.5)
            except queue.Empty:
                continue

            job = self.get(run_id)
            if job is None:
                continue
            self._execute(job)

    def _execute(self, job: Job) -> None:
        self.current_run_id = job.run_id
        job.status = "running"
        job.started_at = time.time()
        job.publish({"type": "status", "status": "running", "question": job.question})

        def on_event(agent: str, message: str) -> None:
            job.publish({"type": "progress", "agent": agent, "message": message})

        try:
            result: report.RunReport = graph.run_research(
                job.question, on_event=on_event, fresh=job.fresh
            )
            job.markdown = result.markdown
            job.approved = result.approved
            job.resumed = result.resumed
            job.stats = _collect_stats(result)

            # Persist before marking the job done. A report held only in this process's
            # memory dies with the next restart, which is exactly how one finished run was
            # already lost.
            try:
                path = report.save(result.question, result.markdown)
                job.report_path = str(path)
                on_event("supervisor", f"report saved to {path.parent.name}/{path.name}")
            except OSError as exc:
                # A failed write must not turn a successful run into a failed one.
                on_event("supervisor", f"could not write report to disk: {exc}")

            job.status = "done"
            job.publish(
                {
                    "type": "done",
                    "approved": result.approved,
                    "resumed": result.resumed,
                    "elapsed_s": round(result.elapsed_s, 1),
                    "report_path": job.report_path,
                }
            )
        except Exception as exc:  # noqa: BLE001 - a failed run must not kill the worker
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
            # Terminal failure event, so a client never has to infer failure from silence.
            job.publish({"type": "error", "error": job.error})
        finally:
            job.finished_at = time.time()
            self.current_run_id = None
            job.close()


def _collect_stats(result: report.RunReport) -> dict:
    """Reuse the CLI's numbers rather than re-deriving them in the API layer."""
    stats = graph.run_stats()
    stats.update(
        {
            "rounds": result.state.rounds_run,
            "critic_revisions": result.state.critic_revisions,
            "sub_questions": len(result.state.plan.sub_questions) if result.state.plan else 0,
            "findings": len(result.state.findings),
            "sources": len(result.state.registry),
            "unique_urls": len(result.state.registry.unique_urls()),
            "elapsed_s": round(result.elapsed_s, 1),
            "total_calls": llm.LEDGER.total_calls,
            "total_tokens": llm.LEDGER.total_tokens,
            "failovers": llm.LEDGER.failover_count(),
            "by_agent": llm.LEDGER.by_agent(),
            "notes": list(result.state.notes),
            "outstanding_issues": [i.model_dump() for i in result.outstanding_issues],
        }
    )
    return stats


MANAGER = JobManager()
