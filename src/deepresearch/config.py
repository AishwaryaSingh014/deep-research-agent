"""Central configuration. Every tunable lives here so behaviour is auditable in one place."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# --- Paths ---
# This file is src/deepresearch/config.py, so the repo root is three levels up. Getting this
# wrong is quiet and expensive: cache/ and outputs/ would be created under src/, orphaning the
# embedding model and every saved report rather than raising an error.
ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = ROOT / "cache"
OUTPUT_DIR = ROOT / "outputs"
CHECKPOINT_DB = CACHE_DIR / "checkpoints.db"

# --- Credentials ---
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "").strip()

# --- Models ---
# Groq is primary: fast, generous free tier, reliable JSON mode.
# Gemini is the fallback because it sits in a *different* rate-limit bucket,
# which is the entire point of having a fallback on free tiers.
# Groq enforces rate limits PER MODEL, so a model chain buys more headroom than the
# cross-provider fallback does: exhausting the 70b's tokens-per-minute window leaves the
# other two untouched. Ordered strongest-first; quality degrades gracefully under load.
GROQ_MODELS = [
    "llama-3.3-70b-versatile",
    "openai/gpt-oss-120b",
    "llama-3.1-8b-instant",
]
GROQ_MODEL = GROQ_MODELS[0]  # primary, referenced in docs and tests
GEMINI_MODEL = "gemini-2.0-flash"

# --- Retry / backoff ---
MAX_ATTEMPTS_PER_PROVIDER = 3
BACKOFF_BASE_SECONDS = 2.0
BACKOFF_MAX_SECONDS = 30.0
# When a provider states its own retry window ("retry in 37s"), obey it up to this bound.
# Generic 2s/4s backoff is guaranteed to fail against a 60s token-per-minute window.
BACKOFF_HINT_MAX_SECONDS = 75.0
LLM_TIMEOUT_SECONDS = 90.0
# Ceiling on total wall-clock for ONE complete() call across every provider and attempt.
# Without this, obeying retry hints means 4 providers x 3 attempts x 75s ~= 15 minutes
# spent on a single call. Observed in practice; hence the bound.
LLM_CALL_DEADLINE_SECONDS = 120.0
# Ceiling on a whole pipeline run, checked between graph nodes.
RUN_DEADLINE_SECONDS = 900.0

# --- Proactive pacing ---
# Groq allows roughly 12k tokens/minute per model on the free tier, and a research run bursts
# far past that. Pacing under the ceiling turns hard 429 failures into small predictable
# pauses, which is strictly better than reacting to rejections after the fact.
TOKENS_PER_MINUTE_BUDGET = 10_000

# --- Hard caps: these are what stop an agent loop from running forever ---
MAX_RESEARCH_ROUNDS = 2
MAX_CRITIC_REVISIONS = 2
MAX_SUBQUESTIONS = 6
MAX_SEARCHES_TOTAL = 12
MAX_URLS_PER_SUBQUESTION = 3
# Caps the Synthesizer's prompt size. Free tiers limit tokens *per minute*, so a prompt that
# grows with the evidence base will eventually trip the limit on a long run.
MAX_FINDINGS_PER_SUBQUESTION = 8
SYNTH_QUOTE_CHARS = 150

# --- Concurrency: keep low enough that free-tier rate limits stay happy ---
MAX_WORKERS = 3

# --- Fetch ---
FETCH_TIMEOUT_SECONDS = 20.0
MAX_PAGE_CHARS = 200_000  # guard against pathological pages before extraction
USER_AGENT = "deep-research-agent/0.1 (+https://github.com/yourname/deep-research-agent)"

# --- Retrieval ---
EMBED_MODEL = "BAAI/bge-small-en-v1.5"  # ONNX via fastembed, ~130MB, no torch
CHUNK_CHARS = 800
CHUNK_OVERLAP = 100
TOP_K_PASSAGES = 5

# --- Generation ---
DEFAULT_TEMPERATURE = 0.2
MAX_OUTPUT_TOKENS = 2048
