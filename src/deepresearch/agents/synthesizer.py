"""Synthesizer: turn scattered findings into a structured, cited report."""

from __future__ import annotations

from .. import config
from ..models import ResearchState
from .base import Agent

SYSTEM_PROMPT = """You write concise, well-sourced research reports in Markdown.

You are given a research question, its sub-questions, and findings. Each finding carries \
source ids like [S3].

Hard rules:
- Every factual sentence must end with the source id(s) it came from, e.g. "... in practice [S3]."
- Use ONLY the source ids supplied. Never invent an id. Never cite an id you were not given.
- Use ONLY the supplied findings. Add no outside knowledge, no matter how confident you feel.
- **ATTRIBUTE, DO NOT GENERALISE.** Each finding shows the source it came from. If a source \
is about one specific system (Cassandra, Milvus, Delta Lake, Databricks, Pinecone...), write \
"Cassandra reclaims space via compaction [S3]" — NEVER "vector databases reclaim space via \
compaction [S3]". Only generalise when several independent sources support the same point, \
and then cite all of them. Over-generalising from a single vendor's documentation is the most \
common way these reports become false, and it will be rejected in review.
- If a source turns out to describe a related but different technology than the question asks \
about, either attribute it explicitly as a contrast ("in Delta Lake, a table format rather \
than a vector index, ...") or omit it. Do not quietly treat it as equivalent.
- Where sources disagree, say so explicitly and cite both.
- If a sub-question has weak or no evidence, say that plainly in the Limitations section \
rather than padding it with generalities.

**If the question rests on a false or unverified premise, say so and stop.** When the findings
do not address what was actually asked — even if they cover a superficially similar topic (a
different company, a different product, a similarly-named event) — the correct report is a
short one that states the premise is unsupported. Do NOT pad it with sections about the
adjacent topic you did find. A two-paragraph "no evidence for this, here is what the sources
actually describe and why it is not the same thing" is a correct and valuable answer; four
sections about the wrong subject is not.

Structure:
# <short descriptive title>

## Summary
Three to five sentences answering the main question directly, with citations.

## <one section per sub-question, using a descriptive heading, not "Q1">
The findings for that sub-question, synthesised into prose. Cite every claim.

## Limitations
Exactly ONE Limitations section, and it must be the last thing you write. Never emit a second
one, and never a "Limitations (continued)".
What the evidence does not establish, which sub-questions were thinly sourced, and any \
disagreement between sources. Be honest and specific.
Write this section in plain prose with NO citations — a statement about what the evidence \
fails to show is not itself a sourced claim, and citing one is a category error.
If some sources turned out to describe a related-but-different topic, say so here explicitly.

Do not write a Sources section — it is appended automatically.
Aim for 700-1200 words. Prefer specificity over length."""


class Synthesizer(Agent):
    name = "synthesizer"
    system_prompt = SYSTEM_PROMPT
    temperature = 0.35
    # A 700-1200 word report is ~1600 tokens. Reserving 3000 inflated every pacing
    # reservation for no benefit.
    max_tokens = 2200

    def _evidence_block(self, state: ResearchState) -> str:
        """Annotate each finding with what its source actually is.

        Without this the model cannot tell that [S16] is a Databricks page and will
        generalise "Databricks does X" into "all vector databases do X" — which is the
        single most common way a cited report becomes wrong.
        """
        blocks: list[str] = []
        self._shown_source_ids: set[str] = set()

        for sub in state.plan.sub_questions if state.plan else []:
            findings = state.findings_for(sub.id)
            if not findings:
                blocks.append(f"### {sub.question}\n  (no evidence found for this sub-question)")
                continue

            kept = findings[: config.MAX_FINDINGS_PER_SUBQUESTION]
            if len(findings) > len(kept):
                # Never truncate silently — a dropped finding is a coverage gap the reader
                # deserves to know about.
                state.note(
                    f"synthesizer: showed {len(kept)}/{len(findings)} findings for {sub.id} "
                    f"(prompt size cap)"
                )

            lines = []
            for finding in kept:
                citations = " ".join(f"[{s}]" for s in finding.source_ids)
                self._shown_source_ids.update(finding.source_ids)
                source = state.registry.get(finding.source_ids[0]) if finding.source_ids else None
                context = f"  (source: {source.title[:60]})" if source else ""
                lines.append(f"  - {finding.claim} {citations}{context}")
            blocks.append(f"### {sub.question}\n" + "\n".join(lines))

        return "\n\n".join(blocks)

    def _sources_block(self, state: ResearchState, only_shown: bool = True) -> str:
        """List citable sources. Restricted to ones backing a shown finding, to bound tokens."""
        shown = getattr(self, "_shown_source_ids", set())
        sources = [
            s
            for s in state.registry.all()
            if not only_shown or not shown or s.id in shown
        ]
        return "\n".join(
            f"[{s.id}] {s.title[:80]} — {s.url}\n"
            f"     \"{s.quote[: config.SYNTH_QUOTE_CHARS].strip()}...\""
            for s in sources
        )

    def run(self, state: ResearchState) -> str:
        self.emit("drafting report")
        prompt = (
            f"Research question: {state.question}\n\n"
            f"## Findings by sub-question\n\n{self._evidence_block(state)}\n\n"
            f"## Available sources (cite by id, these are the ONLY valid ids)\n\n"
            f"{self._sources_block(state)}"
        )
        return self.run_text(prompt).strip()

    def _valid_ids_block(self, state: ResearchState) -> str:
        """Just the citable ids and their titles — no quoted passages.

        The reviser does not need the evidence text: the Critic has already ruled on whether
        each passage supports its claim. All the reviser needs is which ids exist. Sending the
        quotes as well roughly doubled this prompt and was enough to trip the rate limit on a
        free tier, so they are omitted.
        """
        return "\n".join(f"[{s.id}] {s.title[:70]}" for s in state.registry.all())

    def revise(self, state: ResearchState, draft: str, issues: str) -> str:
        """Rewrite the draft to fix the Critic's findings, changing nothing else."""
        self.emit("revising after critique")
        prompt = (
            f"Research question: {state.question}\n\n"
            f"## Your previous draft\n\n{draft}\n\n"
            f"## Problems a reviewer found\n\n{issues}\n\n"
            f"## Valid source ids (cite only these)\n\n{self._valid_ids_block(state)}\n\n"
            "Rewrite the report fixing every problem above.\n\n"
            "How to fix a flagged claim, in order of preference:\n"
            "1. DELETE the sentence entirely. This is almost always the right fix — a shorter, "
            "fully-supported report is strictly better than a longer, shakier one.\n"
            "2. Weaken it until the cited passage genuinely supports it (e.g. 'in Cassandra' "
            "instead of 'in all vector databases').\n"
            "3. Re-cite it to a different valid source that actually supports it.\n\n"
            "Never re-state a flagged claim with the same citation. Never replace it with a "
            "vaguer version of the same assertion. Do not add new claims to compensate for "
            "deleted ones — the report is allowed to get shorter.\n\n"
            "In the Limitations section, describe gaps in plain prose WITHOUT citations: a "
            "statement about what the evidence fails to show is not itself a sourced claim.\n\n"
            "Keep everything not flagged exactly as it was. Output the full corrected report."
        )
        return self.run_text(prompt).strip()
