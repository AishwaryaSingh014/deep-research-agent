"""Frontend constants."""

from __future__ import annotations

import os

API = os.getenv("RESEARCH_API", "http://localhost:8000")

# Agent name -> colour, so the activity feed is scannable rather than a wall of text.
AGENT_COLOURS = {
    "planner": "#7c5cff",
    "searcher": "#0ea5e9",
    "reader": "#10b981",
    "collect": "#f59e0b",
    "gap_analyst": "#ec4899",
    "synthesizer": "#8b5cf6",
    "critic": "#ef4444",
    "supervisor": "#64748b",
}
