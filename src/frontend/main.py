"""Streamlit frontend for the Deep Research Agent.

Composition only: page setup, the question form, and the components. All HTTP lives in
``frontend.api_client``; all rendering lives in ``frontend.components``.

Imports here are absolute (``from frontend...``) rather than relative. Streamlit executes this
file as ``__main__`` with no package context, so a relative import would raise
``ImportError: attempted relative import with no known parent package``. ``src`` is on
PYTHONPATH, which is what makes the absolute form resolve.

Run with:  PYTHONPATH=src streamlit run src/frontend/main.py
"""

from __future__ import annotations

import httpx
import streamlit as st

st.set_page_config(page_title="Deep Research Agent", page_icon="🔎", layout="wide")

from frontend import api_client  # noqa: E402 - must follow set_page_config
from frontend.components import activity, report_view, saved_report, sidebar  # noqa: E402

# Defaults come before the sidebar, which mutates all three from its buttons.
st.session_state.setdefault("run_id", None)
st.session_state.setdefault("viewing_thread", None)
st.session_state.setdefault("streaming", False)

# Fetched once and shared: the sidebar lists it, and the report viewer reads a run's metadata
# out of it. Two components each calling /runs would double the work for no benefit.
try:
    listing = api_client.list_runs()
except Exception as exc:  # noqa: BLE001 - an unreachable API must not blank the page
    listing = {"runs": [], "checkpoints": []}
    st.warning(f"Could not load history: {exc}")

sidebar.render(listing)

st.title("🔎 Deep Research Agent")
st.caption(
    "Six agents plan, search, read and fact-check the web. Every claim carries a citation "
    "that is mechanically verified against the source text."
)

with st.form("ask"):
    col1, col2 = st.columns([5, 1])
    question = col1.text_input(
        "Research question",
        placeholder="How do vector databases handle deletes?",
        label_visibility="collapsed",
    )
    submitted = col2.form_submit_button("Research", use_container_width=True, type="primary")
    fresh = st.checkbox(
        "Ignore saved checkpoint", value=False, help="Start over instead of resuming this question."
    )

if submitted and question.strip():
    try:
        started = api_client.start_research(question, fresh=fresh)
        st.session_state.run_id = started["run_id"]
        st.session_state.viewing_thread = None
        st.session_state.streaming = True
        if started.get("position"):
            st.info(f"Queued at position {started['position']} — one run executes at a time.")
        st.rerun()
    except httpx.HTTPError as exc:
        st.error(f"Could not start the run: {exc}")

# --------------------------------------------------------------------------- #
# Reading a past report takes over the main pane. The form above stays usable, so a new run
# can be started without leaving the one being read.
# --------------------------------------------------------------------------- #
thread_id = st.session_state.viewing_thread
if thread_id:
    summary = next(
        (c for c in listing.get("checkpoints") or [] if c["thread_id"] == thread_id), None
    )
    try:
        payload = api_client.get_checkpoint_report(thread_id)
    except Exception:  # noqa: BLE001
        st.session_state.viewing_thread = None
        st.warning(
            f"No stored report for `{thread_id}` — its checkpoint may have been cleared."
        )
        st.stop()

    # The listing can lag behind (cache cleared between renders), so fall back to the report's
    # own question rather than refusing to display it.
    summary = summary or {"thread_id": thread_id, "question": payload["question"]}
    saved_report.render(summary, payload["markdown"])
    st.stop()

run_id = st.session_state.run_id
if not run_id:
    st.info("Ask a question above, or open a past report from the sidebar.")
    st.stop()

# Streams until the run reaches a terminal state, then reruns; returns immediately otherwise.
activity.render(run_id)

try:
    run = api_client.get_run(run_id)
except Exception:  # noqa: BLE001
    # Most often a run id held over from a previous API process, whose in-memory registry is
    # gone. Clearing it returns the page to a usable state instead of stranding it.
    st.session_state.run_id = None
    st.session_state.streaming = False
    st.info(
        "That run is not in the API's memory any more — it was probably submitted before the "
        "last restart. If it finished, its report is under **Past reports**."
    )
    st.stop()

report_view.render(run)
