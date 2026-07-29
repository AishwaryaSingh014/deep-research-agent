"""FastAPI application factory.

This module wires the service together and nothing else — the handlers live in ``routers/``,
grouped by resource, and the job queue lives in ``services/jobs.py``. Adding an endpoint
should not mean editing the file that configures the application.

Run with:  PYTHONPATH=src uvicorn backend.main:app --reload --port 8000
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .routers import checkpoints, health, reports, research
from .services.jobs import MANAGER


@asynccontextmanager
async def lifespan(_: FastAPI):
    MANAGER.start()
    yield
    MANAGER.stop()


app = FastAPI(
    title="Deep Research Agent",
    description="Multi-agent web research with mechanically verifiable citations.",
    version="0.1.0",
    lifespan=lifespan,
)

# The Streamlit frontend is a separate origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(research.router)
app.include_router(checkpoints.router)
app.include_router(reports.router)
