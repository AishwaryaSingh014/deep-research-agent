"""Typed contracts between agents.

Every agent's output is a pydantic model. An LLM that returns something off-contract fails
validation and gets re-prompted once with the error, rather than silently poisoning the
pipeline with a malformed dict three stages downstream.
"""

from __future__ import annotations

import threading

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- #
# Planner
# --------------------------------------------------------------------------- #
class SubQuestion(BaseModel):
    id: str = Field(description="Stable identifier, e.g. 'Q1'")
    question: str
    search_queries: list[str] = Field(default_factory=list, max_length=2)


class Plan(BaseModel):
    interpretation: str = Field(default="", description="How the agent read the question")
    sub_questions: list[SubQuestion] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Evidence
# --------------------------------------------------------------------------- #
class Source(BaseModel):
    """A citable passage. ``id`` is what appears as [S1] in the report."""

    id: str
    url: str
    title: str
    quote: str = Field(description="Verbatim passage supporting the finding")
    sub_question_id: str = ""


class Finding(BaseModel):
    claim: str
    source_ids: list[str] = Field(default_factory=list)
    sub_question_id: str = ""


class ReaderOutput(BaseModel):
    """What the Reader extracts from one page for one sub-question."""

    findings: list[Finding] = Field(default_factory=list)
    relevant: bool = True


# --------------------------------------------------------------------------- #
# Gap analysis
# --------------------------------------------------------------------------- #
class Gap(BaseModel):
    sub_question_id: str
    reason: str = ""
    followup_queries: list[str] = Field(default_factory=list, max_length=2)


class GapReport(BaseModel):
    sufficient: bool = True
    gaps: list[Gap] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Critique
# --------------------------------------------------------------------------- #
class CriticIssue(BaseModel):
    claim: str
    problem: str
    severity: str = Field(default="minor", description="'major' or 'minor'")


class CriticVerdict(BaseModel):
    approved: bool = True
    issues: list[CriticIssue] = Field(default_factory=list)

    @property
    def major_issues(self) -> list[CriticIssue]:
        return [i for i in self.issues if i.severity.lower() == "major"]


# --------------------------------------------------------------------------- #
# Run-level state
# --------------------------------------------------------------------------- #
class SourceRegistry:
    """Allocates stable [S#] ids and is the single authority on what a citation means.

    The Critic validates against this registry, so a hallucinated [S99] is mechanically
    detectable rather than a matter of opinion.
    """

    def __init__(self) -> None:
        self._sources: dict[str, Source] = {}
        self._by_url_quote: dict[tuple[str, str], str] = {}
        self._counter = 0
        self._lock = threading.Lock()

    def add(self, url: str, title: str, quote: str, sub_question_id: str = "") -> Source:
        key = (url, quote[:200])
        with self._lock:
            if key in self._by_url_quote:
                return self._sources[self._by_url_quote[key]]
            self._counter += 1
            source = Source(
                id=f"S{self._counter}",
                url=url,
                title=title or url,
                quote=quote,
                sub_question_id=sub_question_id,
            )
            self._sources[source.id] = source
            self._by_url_quote[key] = source.id
            return source

    def get(self, source_id: str) -> Source | None:
        return self._sources.get(source_id)

    def exists(self, source_id: str) -> bool:
        return source_id in self._sources

    def all(self) -> list[Source]:
        return sorted(self._sources.values(), key=lambda s: int(s.id[1:]))

    @classmethod
    def from_sources(cls, sources: list[Source] | list[dict]) -> SourceRegistry:
        """Rebuild a registry from serialised state.

        LangGraph checkpoints must serialise, and a registry carries a lock and a counter.
        So the graph stores plain ``Source`` records and the registry is reconstituted around
        them for the lifetime of a single node.
        """
        registry = cls()
        for item in sources:
            source = Source.model_validate(item) if isinstance(item, dict) else item
            registry._sources[source.id] = source
            registry._by_url_quote[(source.url, source.quote[:200])] = source.id
            # Keep the counter ahead of every existing id so new ids never collide.
            registry._counter = max(registry._counter, int(source.id.lstrip("S") or 0))
        return registry

    def unique_urls(self) -> list[tuple[str, str]]:
        seen: dict[str, str] = {}
        for source in self.all():
            seen.setdefault(source.url, source.title)
        return list(seen.items())

    def __len__(self) -> int:
        return len(self._sources)


class ResearchState:
    """Everything accumulated during a run. Passed to the Synthesizer and Critic."""

    def __init__(self, question: str) -> None:
        self.question = question
        self.plan: Plan | None = None
        self.findings: list[Finding] = []
        self.registry = SourceRegistry()
        self.rounds_run = 0
        self.critic_revisions = 0
        self.notes: list[str] = []
        self._lock = threading.Lock()

    def add_findings(self, findings: list[Finding]) -> None:
        with self._lock:
            self.findings.extend(findings)

    def findings_for(self, sub_question_id: str) -> list[Finding]:
        return [f for f in self.findings if f.sub_question_id == sub_question_id]

    def note(self, message: str) -> None:
        with self._lock:
            self.notes.append(message)

    # ----------------------------------------------------------------- #
    # LangGraph adapter
    # ----------------------------------------------------------------- #
    @classmethod
    def from_graph_state(cls, gs: dict) -> ResearchState:
        """Hydrate a working state from the graph's plain-data checkpoint.

        The graph state holds only serialisable primitives. Agents expect the rich object,
        so each node hydrates one, uses it, and returns updates. This is what allows the
        orchestrator to change without touching a single agent.
        """
        state = cls(gs.get("question", ""))
        plan = gs.get("plan")
        if plan:
            state.plan = Plan.model_validate(plan) if isinstance(plan, dict) else plan
        state.findings = [
            Finding.model_validate(f) if isinstance(f, dict) else f
            for f in gs.get("findings") or []
        ]
        state.registry = SourceRegistry.from_sources(gs.get("sources") or [])
        state.rounds_run = gs.get("round_index", 0)
        state.critic_revisions = gs.get("critic_revisions", 0)
        state.notes = list(gs.get("notes") or [])
        return state

    def new_notes(self, previous: list[str]) -> list[str]:
        """Notes added during this node only — the graph reducer appends, so don't resend."""
        return self.notes[len(previous) :]

    def sources_as_dicts(self) -> list[dict]:
        return [s.model_dump() for s in self.registry.all()]
