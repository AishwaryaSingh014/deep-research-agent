"""Every HTTP call to the backend lives here.

The UI talks to the FastAPI service over HTTP rather than importing ``deepresearch`` directly.
That is deliberate: the service boundary is the point. It also means the UI stays responsive
while a run takes minutes in another process.

Keeping the calls in one module means no component builds a URL itself, so the API surface
the frontend depends on can be read in one place.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import httpx

from frontend.config import API

TIMEOUT = 30.0


def _get(path: str, **kwargs):
    # raise_for_status matters here: without it a 404 body ({"detail": ...}) is returned as if
    # it were the resource, and the caller fails much later on a missing key. A run id left in
    # session state by a previous API process used to crash the page that way.
    response = httpx.get(f"{API}{path}", timeout=TIMEOUT, **kwargs)
    response.raise_for_status()
    return response.json()


def _post(path: str, **kwargs):
    response = httpx.post(f"{API}{path}", timeout=TIMEOUT, **kwargs)
    response.raise_for_status()
    return response.json()


def health() -> dict | None:
    """Backend configuration, or None when the API cannot be reached at all."""
    try:
        return _get("/health")
    except Exception:  # noqa: BLE001 - an unreachable backend is a UI state, not an error
        return None


def list_runs() -> dict:
    return _get("/runs")


def get_run(run_id: str) -> dict:
    return _get(f"/research/{run_id}")


def start_research(question: str, fresh: bool = False) -> dict:
    return _post("/research", json={"question": question, "fresh": fresh})


def resume_run(run_id: str) -> dict:
    return _post(f"/research/{run_id}/resume")


def get_checkpoint_report(thread_id: str) -> dict:
    """The finished report of a past run: ``{thread_id, question, markdown}``.

    Served from the checkpoint database, so this works for runs whose API process is long gone
    — which is most of them.
    """
    return _get(f"/checkpoints/{thread_id}/report")


def save_checkpoint(thread_id: str) -> dict:
    return _post(f"/checkpoints/{thread_id}/save")


def stream_events(run_id: str) -> Iterator[dict]:
    """Yield decoded SSE events until the run reaches a terminal state."""
    url = f"{API}/research/{run_id}/events"
    with httpx.stream("GET", url, timeout=None) as response:
        for line in response.iter_lines():
            if not line or line.startswith(":"):
                continue  # keep-alive comment
            if line.startswith("data: "):
                try:
                    yield json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
