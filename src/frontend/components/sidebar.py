"""Sidebar: backend health, this session's runs, and every past report on disk."""

from __future__ import annotations

import streamlit as st

from frontend import api_client
from frontend.config import API


def _render_health() -> None:
    st.markdown("### Backend")
    info = api_client.health()
    if info is None:
        st.error(f"Cannot reach the API at {API}")
        st.caption("Start it with `./run.sh api`")
        return

    st.success("connected" if info["can_run"] else "connected, but no LLM key")
    for name, configured in info["providers"].items():
        st.markdown(f"{'✅' if configured else '⚪'} {name}")
    if info["worker_busy"]:
        st.info("worker busy — new runs will queue")
    with st.expander("Limits"):
        st.json(info["limits"])


def _render_session_runs(runs: list[dict]) -> None:
    """Runs submitted to the *current* API process.

    This list lives in memory and is empty after a restart. Past reports come from the
    checkpoint database instead — see ``_render_saved_reports``.
    """
    if not runs:
        return
    st.markdown("### This session")
    for run in runs[:8]:
        icon = {"done": "✅", "failed": "❌", "running": "⏳", "queued": "🕓"}.get(
            run["status"], "•"
        )
        if st.button(
            f"{icon} {run['question'][:38]}",
            key=f"run-{run['run_id']}",
            use_container_width=True,
        ):
            st.session_state.run_id = run["run_id"]
            st.session_state.viewing_thread = None
            st.session_state.streaming = run["status"] in ("queued", "running")
            st.rerun()


def _render_saved_reports(checkpoints: list[dict]) -> None:
    """Every finished report on disk, and any run that stopped partway.

    A checkpoint's row count says nothing about progress — it is the same for a finished run and
    one that died at the first node. ``complete`` (derived from the graph's ``next``) is the only
    honest signal, so it is what splits the two groups.
    """
    finished = [c for c in checkpoints if c["complete"] and c["report_chars"]]
    unfinished = [c for c in checkpoints if not c["complete"]]

    # Longest report first, so when one question has several threads (a --fresh re-run makes a
    # second one) the substantive report is the obvious pick.
    finished.sort(key=lambda c: c["report_chars"], reverse=True)

    st.markdown("### Past reports")
    if not finished:
        st.caption("No finished reports yet.")
    else:
        st.caption(f"{len(finished)} saved — click to read.")

    # Questions that appear more than once need their thread id shown to be distinguishable.
    seen: dict[str, int] = {}
    for cp in finished:
        seen[cp["question"]] = seen.get(cp["question"], 0) + 1

    for cp in finished:
        flag = "✅" if cp["approved"] else "⚠️"
        if st.button(
            f"{flag} {cp['question'][:40]}",
            key=f"view-{cp['thread_id']}",
            use_container_width=True,
        ):
            st.session_state.viewing_thread = cp["thread_id"]
            st.session_state.run_id = None
            st.session_state.streaming = False
            st.rerun()
        detail = f"{cp['sources']} sources · {cp['report_chars']:,} chars"
        if seen[cp["question"]] > 1:
            detail += f" · `{cp['thread_id'][-10:]}`"
        st.caption(detail)

    if unfinished:
        with st.expander(f"⏸ Unfinished ({len(unfinished)})"):
            st.caption("Stopped mid-pipeline — no report yet. Re-ask to resume from here.")
            for cp in unfinished:
                st.caption(f"**{cp['question'][:40]}** — next: `{', '.join(cp['next'])}`")


def render(listing: dict) -> None:
    """``listing`` is the /runs payload, fetched once by main.py and shared with the viewer."""
    with st.sidebar:
        _render_health()
        st.markdown("---")
        _render_session_runs(listing.get("runs") or [])
        _render_saved_reports(listing.get("checkpoints") or [])
