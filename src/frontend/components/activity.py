"""The live agent feed.

Watching `reader  4 findings from ...` scroll past is what makes the system legible in ten
seconds, so this is the part of the UI that earns the SSE plumbing behind it.
"""

from __future__ import annotations

import streamlit as st

from frontend import api_client
from frontend.config import AGENT_COLOURS


def _line(agent: str, message: str) -> str:
    colour = AGENT_COLOURS.get(agent, "#64748b")
    return (
        "<div style='font-family:ui-monospace,monospace;font-size:0.84rem;'>"
        f"<span style='color:{colour};font-weight:600'>{agent:<12}</span>"
        f"<span style='opacity:.85'>{message}</span></div>"
    )


def render(run_id: str) -> None:
    """Consume the run's event stream until it reaches a terminal state, then rerun.

    Only called while ``st.session_state.streaming`` is set. A dropped stream is reported but
    never fails the run — the work continues on the server either way.
    """
    st.markdown("### Agent activity")
    feed = st.container(height=280)

    if not st.session_state.streaming:
        return

    lines: list[str] = []
    terminal: dict | None = None
    try:
        for event in api_client.stream_events(run_id):
            if event.get("type") == "progress":
                lines.append(_line(event["agent"], event["message"]))
                feed.markdown("".join(lines[-400:]), unsafe_allow_html=True)
            elif event.get("type") in ("done", "error"):
                terminal = event
                break
    except Exception as exc:  # noqa: BLE001 - a dropped stream must not lose the run
        st.warning(f"Progress stream interrupted ({exc}). The run continues on the server.")

    st.session_state.streaming = False
    if terminal and terminal.get("type") == "error":
        st.error(terminal["error"])
    st.rerun()
