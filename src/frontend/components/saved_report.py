"""Reading a past report.

The report and its metadata come from the checkpoint database, not from the API's in-memory job
registry, so a run from weeks ago reads exactly like one from a minute ago.
"""

from __future__ import annotations

import httpx
import streamlit as st

from frontend import api_client


def _back_button() -> None:
    if st.button("← Back to current run"):
        st.session_state.viewing_thread = None
        st.rerun()


def render(summary: dict, markdown: str) -> None:
    """``summary`` is one entry from /runs' ``checkpoints``; ``markdown`` the finished report."""
    _back_button()

    st.subheader(summary["question"])
    st.caption(f"from checkpoint `{summary['thread_id']}`")

    cols = st.columns(5)
    cols[0].metric("Sources", summary.get("sources", 0))
    cols[1].metric("Findings", summary.get("findings", 0))
    cols[2].metric("Revisions", summary.get("critic_revisions", 0))
    cols[3].metric("Open issues", summary.get("outstanding_issues", 0))
    cols[4].metric("Length", f"{len(markdown):,}")

    if summary.get("approved"):
        st.success("✅ Citations verified — the fact-checker raised no unresolved issues.")
    else:
        st.warning(
            "⚠️ Unresolved citation issues — the report ends with a reviewer note listing "
            "each disputed claim."
        )

    report_tab, raw_tab = st.tabs(["Report", "Markdown"])

    with report_tab:
        st.markdown(markdown, unsafe_allow_html=False)

    with raw_tab:
        st.code(markdown, language="markdown")
        left, right = st.columns(2)
        left.download_button(
            "Download report",
            markdown,
            file_name=f"{summary['thread_id']}.md",
            mime="text/markdown",
            use_container_width=True,
        )
        # Reports finished before auto-saving existed are not in outputs/ yet. Writing is an
        # idempotent overwrite, so this needs no is-it-already-there check.
        if right.button("Save to outputs/", use_container_width=True):
            try:
                saved = api_client.save_checkpoint(summary["thread_id"])
                st.success(f"Wrote outputs/{saved['filename']}")
            except httpx.HTTPError as exc:
                st.error(f"Could not save: {exc}")
