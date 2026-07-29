"""Configuration and liveness."""

from __future__ import annotations

from fastapi import APIRouter

from deepresearch import config

from ..services.jobs import MANAGER

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict:
    """Configuration check. Useful before submitting a job that would fail on a missing key."""
    return {
        "status": "ok",
        "providers": {
            "groq": bool(config.GROQ_API_KEY),
            "gemini": bool(config.GEMINI_API_KEY),
            "tavily": bool(config.TAVILY_API_KEY),
        },
        "models": config.GROQ_MODELS + [config.GEMINI_MODEL],
        "can_run": bool(config.GROQ_API_KEY or config.GEMINI_API_KEY),
        "worker_busy": MANAGER.current_run_id is not None,
        "limits": {
            "max_research_rounds": config.MAX_RESEARCH_ROUNDS,
            "max_critic_revisions": config.MAX_CRITIC_REVISIONS,
            "max_searches_total": config.MAX_SEARCHES_TOTAL,
            "tokens_per_minute_budget": config.TOKENS_PER_MINUTE_BUDGET,
        },
    }
