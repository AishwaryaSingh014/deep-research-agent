"""Run-scoped clock, shared by the orchestrator and any agent that loops internally.

The whole-run deadline used to live in ``graph.py`` and was only consulted between nodes.
That was not enough once the Critic began batching: a single node making six LLM calls can
overrun the deadline without the graph ever getting a chance to notice. Any agent that loops
internally checks ``expired()`` too.

Kept in its own module so agents can import it without importing the graph that imports them.
"""

from __future__ import annotations

import time

from . import config

_STARTED = time.monotonic()


def start() -> None:
    """Reset the clock at the beginning of a run."""
    global _STARTED
    _STARTED = time.monotonic()


def elapsed() -> float:
    return time.monotonic() - _STARTED


def expired() -> bool:
    """True once the run has exceeded ``RUN_DEADLINE_SECONDS``.

    Measured per process rather than stored in graph state, so resuming a checkpointed run
    grants a fresh budget instead of instantly timing out on the original start time.
    """
    return elapsed() > config.RUN_DEADLINE_SECONDS


def remaining() -> float:
    return max(0.0, config.RUN_DEADLINE_SECONDS - elapsed())
