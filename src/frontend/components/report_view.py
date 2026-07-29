"""Terminal states of a run: queued, running, failed, and the finished report."""

from __future__ import annotations

import time

import httpx
import streamlit as st

from frontend import api_client


def _render_queued(run: dict) -> None:
    st.info(f"Queued at position {run.get('position', 0)}.")
    time.sleep(2)
    st.rerun()


def _render_running() -> None:
    st.info("Running…")
    if st.button("Reattach to live feed"):
        st.session_state.streaming = True
        st.rerun()


def _render_failed(run: dict) -> None:
    st.error(run["error"] or "The run failed.")
    if not run.get("resumable"):
        return
    st.info(
        "The completed nodes of this run are checkpointed — resuming continues from "
        "where it stopped instead of repeating the research."
    )
    if st.button("Resume from checkpoint", type="primary"):
        try:
            resumed = api_client.resume_run(run["run_id"])
            st.session_state.run_id = resumed["run_id"]
            st.session_state.streaming = True
            st.rerun()
        except httpx.HTTPError as exc:
            st.error(f"Could not resume: {exc}")


def _render_stats(stats: dict) -> None:
    left, right = st.columns(2)
    with left:
        st.markdown("**Pipeline**")
        st.json(
            {
                k: stats.get(k)
                for k in (
                    "rounds",
                    "critic_revisions",
                    "sub_questions",
                    "findings",
                    "sources",
                    "unique_urls",
                    "searches_used",
                    "fetch_failures",
                    "embeddings_active",
                )
            }
        )
    with right:
        st.markdown("**LLM usage**")
        st.json(
            {
                "total_calls": stats.get("total_calls"),
                "total_tokens": stats.get("total_tokens"),
                "failovers": stats.get("failovers"),
                "throttle_pauses": stats.get("throttle_pauses"),
                "by_agent": stats.get("by_agent"),
            }
        )
    if stats.get("notes"):
        st.markdown("**Degradation notes**")
        for note in stats["notes"]:
            st.caption(f"• {note}")


def _render_done(run: dict) -> None:
    stats = run.get("stats") or {}

    badge = "✅ citations verified" if run["approved"] else "⚠️ unresolved citation issues"
    cols = st.columns(5)
    cols[0].metric("Status", badge.split(" ", 1)[1][:18])
    cols[1].metric("Sources", stats.get("sources", 0))
    cols[2].metric("Findings", stats.get("findings", 0))
    cols[3].metric("Tokens", f"{stats.get('total_tokens', 0):,}")
    cols[4].metric("Elapsed", f"{stats.get('elapsed_s', 0):.0f}s")

    if run.get("resumed"):
        st.success("This run resumed from a checkpoint — completed nodes were not repeated.")
    if run.get("report_path"):
        st.caption(f"📄 Saved to `{run['report_path']}`")
    else:
        st.warning("This report was not written to disk — it exists only in the API process.")

    report_tab, stats_tab, raw_tab = st.tabs(["Report", "Run stats", "Markdown"])
    with report_tab:
        st.markdown(run["markdown"], unsafe_allow_html=False)
    with stats_tab:
        _render_stats(stats)
    with raw_tab:
        st.code(run["markdown"], language="markdown")
        st.download_button(
            "Download report",
            run["markdown"],
            file_name=f"{run['run_id']}.md",
            mime="text/markdown",
        )


def render(run: dict) -> None:
    if not st.session_state.streaming and run["event_count"]:
        st.caption(f"{run['event_count']} events · status: {run['status']}")

    handler = {
        "queued": lambda: _render_queued(run),
        "running": _render_running,
        "failed": lambda: _render_failed(run),
        "done": lambda: _render_done(run),
    }.get(run["status"])
    if handler:
        handler()
