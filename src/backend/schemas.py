"""Request models for the API.

Responses are deliberately plain dicts rather than pydantic models. The payloads are assembled
from ``Job.to_dict`` and ``graph.run_stats()``, which already own their shape; declaring a
second schema here would mean two definitions to keep in sync and would silently drop any key
a response model did not know about.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ResearchRequest(BaseModel):
    question: str = Field(..., min_length=8, max_length=500)
    fresh: bool = Field(
        False, description="Ignore any saved checkpoint for this question and start over."
    )
