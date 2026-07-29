"""LangGraph orchestration.

Replaces the original hand-written supervisor loop. To be clear about why: the graph DSL is
*not* clearer than the loop it replaced — the control flow is two cycles and a branch. The
migration buys exactly one thing, and it is the thing the loop could not do: **checkpointed
state**. Runs were dying at the final node under free-tier rate limits and discarding twenty
minutes of completed research. Now they resume.

Two design points worth knowing before reading on:

1. **Agents are untouched.** They still take the rich ``ResearchState``. Each node hydrates one
   from the plain-data checkpoint, uses it, and returns updates. The orchestrator changed
   without a single agent changing, which is the payoff for having typed contracts.

2. **Citation ids are assigned after the fan-out, not during it.** Parallel readers sharing a
   global counter would both allocate ``[S5]``. Each read therefore gets a private registry,
   and ``collect`` renumbers deterministically by job index — so ids depend on the work, not on
   which thread happened to finish first. Resumed runs produce identical citations.
"""

from __future__ import annotations

import operator
import sqlite3
import threading
import time
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from . import config, llm, report, runtime
from .agents.critic import Critic
from .agents.gap_analyst import GapAnalyst
from .agents.planner import Planner
from .agents.reader import Reader
from .agents.searcher import Searcher
from .agents.synthesizer import Synthesizer
from .models import CriticIssue, Finding, ResearchState, Source, SourceRegistry
from .tools import fetch, rank, search

# The progress callback is process-global rather than threaded through state: it is a UI
# concern, and graph state must stay serialisable.
#
# A ContextVar would be the tidier answer, but it would not actually work: the read fan-out
# runs in worker threads that do not inherit the caller's context. So the callback is a plain
# global, and `_RUN_LOCK` makes the "one run per process" assumption explicit and enforced
# rather than merely documented. Callers that need concurrency must run separate processes;
# the API layer serialises through a job queue for the same reason.
_ON_EVENT = None
_RUN_STARTED = 0.0
_RUN_LOCK = threading.Lock()


def _emit(agent: str, message: str) -> None:
    if _ON_EVENT:
        _ON_EVENT(agent, message)


def _expired() -> bool:
    """Whole-run deadline. Lives in ``runtime`` so agents that loop internally share it."""
    return runtime.expired()


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
def _merge_reads(left: list | None, right: list | None) -> list:
    """Append reads, or reset when a node explicitly returns ``None``.

    ``collect`` consumes the batch and clears it so the next round starts empty; a plain
    ``operator.add`` reducer has no way to express that.
    """
    if right is None:
        return []
    return (left or []) + list(right)


class GraphState(TypedDict, total=False):
    question: str
    plan: dict | None
    findings: Annotated[list[dict], operator.add]
    sources: list[dict]
    raw_reads: Annotated[list[dict], _merge_reads]
    round_index: int
    tried_queries: list[str]
    seen_urls: list[str]
    targets: list[dict]
    pending: list[dict]
    sufficient: bool
    draft: str
    critic_issues: list[dict]
    critic_revisions: int
    approved: bool
    notes: Annotated[list[str], operator.add]


# --------------------------------------------------------------------------- #
# Nodes
# --------------------------------------------------------------------------- #
def node_plan(state: GraphState) -> dict:
    rs = ResearchState.from_graph_state(state)
    plan = Planner(_emit).run(state["question"], state=rs)
    return {
        "plan": plan.model_dump(),
        "targets": [
            {"sub_question_id": s.id, "queries": s.search_queries} for s in plan.sub_questions
        ],
        "round_index": 0,
        "notes": rs.new_notes([]),
    }


def node_search(state: GraphState) -> dict:
    rs = ResearchState.from_graph_state(state)
    searcher = Searcher(_emit)

    by_id = {s.id: s for s in rs.plan.sub_questions} if rs.plan else {}
    seen = set(state.get("seen_urls") or [])
    tried = set(state.get("tried_queries") or [])

    pending: list[dict] = []
    for target in state.get("targets") or []:
        sub = by_id.get(target["sub_question_id"])
        if not sub:
            continue
        queries = target.get("queries") or [sub.question]
        tried.update(q.strip().lower() for q in queries)
        for result in searcher.run(sub.question, queries, seen):
            pending.append(
                {
                    "index": len(pending),
                    "sub_question_id": sub.id,
                    "sub_question": sub.question,
                    "result": result.__dict__,
                }
            )

    return {
        "pending": pending,
        "seen_urls": sorted(seen),
        "tried_queries": sorted(tried),
        "round_index": state.get("round_index", 0) + 1,
    }


def node_read(payload: dict) -> dict:
    """One source, one sub-question. Runs as a parallel Send branch.

    Uses a *private* registry so concurrent branches cannot collide on citation ids;
    ``node_collect`` renumbers everything into the global sequence afterwards.
    """
    job = payload["job"]
    rs = ResearchState(payload.get("question", ""))
    local = SourceRegistry()

    result = search.SearchResult(**job["result"])
    findings = Reader(_emit).run(
        job["sub_question_id"], job["sub_question"], result, local, state=rs
    )

    return {
        "raw_reads": [
            {
                "index": job["index"],
                "findings": [f.model_dump() for f in findings],
                "sources": [s.model_dump() for s in local.all()],
            }
        ],
        "notes": rs.new_notes([]),
    }


def node_collect(state: GraphState) -> dict:
    """Join the fan-out and assign stable global citation ids.

    Sorting by job index rather than completion order is what makes ids reproducible across
    runs and across resumes.
    """
    registry = SourceRegistry.from_sources(state.get("sources") or [])
    new_findings: list[dict] = []

    for batch in sorted(state.get("raw_reads") or [], key=lambda b: b["index"]):
        local_by_id = {s["id"]: s for s in batch["sources"]}
        for raw in batch["findings"]:
            finding = Finding.model_validate(raw)
            global_ids: list[str] = []
            for local_id in finding.source_ids:
                payload = local_by_id.get(local_id)
                if not payload:
                    continue
                source = Source.model_validate(payload)
                global_ids.append(
                    registry.add(
                        url=source.url,
                        title=source.title,
                        quote=source.quote,
                        sub_question_id=source.sub_question_id,
                    ).id
                )
            if not global_ids:
                continue  # an uncitable finding is not a finding
            finding.source_ids = sorted(set(global_ids), key=lambda i: int(i[1:]))
            new_findings.append(finding.model_dump())

    _emit("collect", f"{len(new_findings)} findings, {len(registry)} sources")
    return {
        "findings": new_findings,
        "sources": [s.model_dump() for s in registry.all()],
        "raw_reads": None,  # clear the batch for the next round
        "pending": [],
    }


def node_gap_check(state: GraphState) -> dict:
    # Skip the LLM call entirely when no further round is possible.
    if state.get("round_index", 0) >= config.MAX_RESEARCH_ROUNDS or _expired():
        return {"sufficient": True, "targets": []}

    rs = ResearchState.from_graph_state(state)
    gap_report = GapAnalyst(_emit).run(rs, set(state.get("tried_queries") or []))
    return {
        "sufficient": gap_report.sufficient,
        "targets": [
            {"sub_question_id": g.sub_question_id, "queries": g.followup_queries}
            for g in gap_report.gaps
        ],
        "notes": rs.new_notes(list(state.get("notes") or [])),
    }


def node_synthesize(state: GraphState) -> dict:
    rs = ResearchState.from_graph_state(state)
    draft = Synthesizer(_emit).run(rs)
    return {"draft": draft, "notes": rs.new_notes(list(state.get("notes") or []))}


def node_critique(state: GraphState) -> dict:
    rs = ResearchState.from_graph_state(state)
    verdict = Critic(_emit).run(rs, state.get("draft", ""))
    return {
        "approved": verdict.approved,
        "critic_issues": [i.model_dump() for i in verdict.major_issues],
        "notes": rs.new_notes(list(state.get("notes") or [])),
    }


def node_revise(state: GraphState) -> dict:
    rs = ResearchState.from_graph_state(state)
    issues = [CriticIssue.model_validate(i) for i in state.get("critic_issues") or []]
    formatted = "\n".join(
        f"- ({i.severity}) {i.claim}\n  Problem: {i.problem}" for i in issues
    )
    draft = Synthesizer(_emit).revise(rs, state.get("draft", ""), formatted)
    return {
        "draft": draft,
        "critic_revisions": state.get("critic_revisions", 0) + 1,
        "notes": rs.new_notes(list(state.get("notes") or [])),
    }


def node_insufficient(state: GraphState) -> dict:
    rs = ResearchState.from_graph_state(state)
    _emit("supervisor", "no usable evidence — refusing to fabricate")
    return {"draft": report.insufficient_evidence_report(rs), "approved": True}


def node_finalize(state: GraphState) -> dict:
    rs = ResearchState.from_graph_state(state)
    issues = [CriticIssue.model_validate(i) for i in state.get("critic_issues") or []]
    body = report.strip_model_sources_section(state.get("draft", ""))
    markdown = body + "\n" + report.sources_section(rs)
    if not state.get("approved", False):
        markdown += report.reviewer_note(issues, state.get("critic_revisions", 0))
    return {"draft": markdown}


# --------------------------------------------------------------------------- #
# Routers — every one of these MUST have a terminal branch
# --------------------------------------------------------------------------- #
def route_after_search(state: GraphState) -> Any:
    """Fan out one branch per source, or skip straight to the join if nothing was found."""
    pending = state.get("pending") or []
    if not pending:
        return "collect"
    return [
        Send("read", {"job": job, "question": state["question"]}) for job in pending
    ]


def route_after_gap(state: GraphState) -> str:
    done = (
        state.get("sufficient", True)
        or state.get("round_index", 0) >= config.MAX_RESEARCH_ROUNDS
        or not state.get("targets")
        or _expired()
    )
    if not done:
        return "search"
    # Out of rounds: with nothing to cite, refuse rather than invent.
    return "synthesize" if state.get("findings") else "insufficient"


def route_after_critique(state: GraphState) -> str:
    if state.get("approved", False):
        return "finalize"
    if state.get("critic_revisions", 0) >= config.MAX_CRITIC_REVISIONS or _expired():
        return "finalize"
    return "revise"


# --------------------------------------------------------------------------- #
# Graph
# --------------------------------------------------------------------------- #
def build_graph(checkpointer=None):
    builder = StateGraph(GraphState)

    builder.add_node("plan", node_plan)
    builder.add_node("search", node_search)
    builder.add_node("read", node_read)
    builder.add_node("collect", node_collect)
    builder.add_node("gap_check", node_gap_check)
    builder.add_node("synthesize", node_synthesize)
    builder.add_node("critique", node_critique)
    builder.add_node("revise", node_revise)
    builder.add_node("insufficient", node_insufficient)
    builder.add_node("finalize", node_finalize)

    builder.add_edge(START, "plan")
    builder.add_edge("plan", "search")
    builder.add_conditional_edges("search", route_after_search, ["read", "collect"])
    builder.add_edge("read", "collect")
    builder.add_edge("collect", "gap_check")
    builder.add_conditional_edges(
        "gap_check", route_after_gap, ["search", "synthesize", "insufficient"]
    )
    builder.add_edge("synthesize", "critique")
    builder.add_conditional_edges("critique", route_after_critique, ["revise", "finalize"])
    builder.add_edge("revise", "critique")
    builder.add_edge("insufficient", END)
    builder.add_edge("finalize", END)

    return builder.compile(checkpointer=checkpointer)


def _make_checkpointer():
    from langgraph.checkpoint.sqlite import SqliteSaver

    config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False because Send fan-out writes checkpoints from worker threads.
    conn = sqlite3.connect(str(config.CHECKPOINT_DB), check_same_thread=False)
    saver = SqliteSaver(conn)
    saver.setup()
    return saver, conn


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def run_research(question: str, on_event=None, fresh: bool = False) -> report.RunReport:
    """Execute the pipeline, resuming from checkpoint when one exists for this question.

    Serialised process-wide: the progress callback and the token/search counters are module
    globals, so two concurrent runs would interleave each other's output. Free tiers cannot
    sustain two research runs anyway, so this costs nothing real.
    """
    global _ON_EVENT, _RUN_STARTED

    with _RUN_LOCK:
        return _run_research_locked(question, on_event, fresh)


def _run_research_locked(question: str, on_event, fresh: bool) -> report.RunReport:
    global _ON_EVENT, _RUN_STARTED
    _ON_EVENT = on_event
    _RUN_STARTED = time.monotonic()
    runtime.start()
    started = time.monotonic()

    search.BUDGET.reset()
    llm.reset_limiters()
    llm.LEDGER.reset()
    fetch.STATS.hits = fetch.STATS.misses = fetch.STATS.failures = 0

    checkpointer, conn = _make_checkpointer()
    try:
        graph = build_graph(checkpointer)

        # Deriving the thread id from the question means re-running the same question
        # resumes it automatically — no flag needed for the common case.
        thread_id = report.slugify(question)
        if fresh:
            thread_id = f"{thread_id}-{int(time.time())}"

        run_config = {
            "configurable": {"thread_id": thread_id},
            "max_concurrency": config.MAX_WORKERS,
            "recursion_limit": 100,
        }

        existing = graph.get_state(run_config) if not fresh else None
        resumed = bool(existing and existing.values and existing.next)
        if resumed:
            _emit("supervisor", f"resuming from checkpoint (next: {', '.join(existing.next)})")

        initial: GraphState = {
            "question": question,
            "findings": [],
            "sources": [],
            "raw_reads": [],
            "notes": [],
            "round_index": 0,
            "critic_revisions": 0,
            "approved": False,
            "tried_queries": [],
            "seen_urls": [],
        }
        # Passing None resumes an interrupted thread from its checkpoint instead of restarting.
        final = graph.invoke(None if resumed else initial, config=run_config)
    finally:
        conn.close()

    rs = ResearchState.from_graph_state(final)
    return report.RunReport(
        question=question,
        markdown=final.get("draft", ""),
        state=rs,
        elapsed_s=time.monotonic() - started,
        approved=final.get("approved", False),
        outstanding_issues=[
            CriticIssue.model_validate(i) for i in final.get("critic_issues") or []
        ],
        resumed=resumed,
    )


# --------------------------------------------------------------------------- #
# Checkpoint inspection
# --------------------------------------------------------------------------- #
def _thread_ids(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT DISTINCT thread_id FROM checkpoints ORDER BY thread_id").fetchall()
    return [r[0] for r in rows]


def checkpoint_summaries() -> list[dict]:
    """Describe every checkpointed thread by what the graph would do next.

    The number of rows a thread has in the checkpoints table says nothing about progress —
    every thread here has the same count whether it finished or died at the first node. The
    only honest completion signal is ``state.next``: empty means the graph reached END.
    """
    if not config.CHECKPOINT_DB.exists():
        return []

    try:
        checkpointer, conn = _make_checkpointer()
    except sqlite3.Error:
        return []

    summaries: list[dict] = []
    try:
        graph = build_graph(checkpointer)
        for thread_id in _thread_ids(conn):
            try:
                state = graph.get_state({"configurable": {"thread_id": thread_id}})
            except Exception:  # noqa: BLE001 - one unreadable thread must not hide the rest
                continue
            values = state.values or {}
            pending = list(state.next or ())
            summaries.append(
                {
                    "thread_id": thread_id,
                    "question": values.get("question") or thread_id,
                    "complete": not pending,
                    "next": pending,
                    "approved": bool(values.get("approved")),
                    "findings": len(values.get("findings") or []),
                    "sources": len(values.get("sources") or []),
                    "critic_revisions": values.get("critic_revisions", 0),
                    "outstanding_issues": len(values.get("critic_issues") or []),
                    # A complete thread's draft is the finalized markdown: node_finalize
                    # writes the assembled report back into `draft`.
                    "report_chars": len(values.get("draft") or "") if not pending else 0,
                }
            )
    except sqlite3.Error:
        return summaries
    finally:
        conn.close()
    return summaries


def checkpoint_report(thread_id: str) -> tuple[str, str] | None:
    """Return ``(question, markdown)`` for a completed thread, or None.

    Recovers a report from a run whose process has since exited — the research is on disk
    even when the in-memory job registry is gone.
    """
    if not config.CHECKPOINT_DB.exists():
        return None
    checkpointer, conn = _make_checkpointer()
    try:
        graph = build_graph(checkpointer)
        state = graph.get_state({"configurable": {"thread_id": thread_id}})
        if state.next:
            return None  # still mid-pipeline; there is no finalized report yet
        values = state.values or {}
        draft = values.get("draft") or ""
        if not draft:
            return None
        return values.get("question") or thread_id, draft
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def run_stats() -> dict:
    """Non-LLM run statistics, surfaced by ``--verbose``."""
    return {
        "searches_used": search.BUDGET.used,
        "searches_limit": search.BUDGET.limit,
        "fetch_hits": fetch.STATS.hits,
        "fetch_misses": fetch.STATS.misses,
        "fetch_failures": fetch.STATS.failures,
        "fetch_hit_rate": fetch.STATS.hit_rate,
        "embeddings_active": rank.using_embeddings(),
        "throttle_pauses": llm.limiter_stats()[0],
        "throttle_seconds": llm.limiter_stats()[1],
    }
