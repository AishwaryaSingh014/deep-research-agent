"""Critic: verify that the report's citations are real and that they support their claims.

Two layers, deliberately:

1. **Mechanical** (Python, always correct): extract every [S#] in the draft with a regex and
   check it against the SourceRegistry. A hallucinated [S99] is caught with certainty — no
   model judgement involved.

2. **Semantic** (LLM): does the cited passage actually support the sentence attached to it?
   This needs judgement, so a model does it — but only after the cheap check has run.

Layer 1 is what makes the grounding claim verifiable rather than aspirational.
"""

from __future__ import annotations

import re

from .. import runtime
from ..models import CriticIssue, CriticVerdict, ResearchState
from .base import Agent

CITATION_RE = re.compile(r"\[(S\d+)\]")
# Everything from the Sources heading onward is generated, not asserted.
_TRAILING_SECTION_RE = re.compile(r"^#{1,6}\s*(sources|references)\b", re.IGNORECASE)
# A bibliography line: "- **[S3]** [Title](url)"
_SOURCE_ENTRY_RE = re.compile(r"^\*{0,2}\[S\d+\]\*{0,2}\s*\[", re.IGNORECASE)

SYSTEM_PROMPT = """You are a fact-checking reviewer. You are adversarial: your job is to find \
claims the sources do not support, not to praise the report.

You are given a list of claims and the verbatim passages they cite.

Flag a claim when:
- the cited passage does not actually support it ("major"),
- it is stated more strongly or more generally than the passage warrants — e.g. the passage \
describes one product and the claim generalises to a whole category ("major"),
- it is vague or hedged to the point of being uninformative ("minor").

Do NOT flag a claim just because the passage is worded differently; faithful paraphrase is \
correct and expected. Only flag a real mismatch in meaning or scope.

**Your "problem" field must be specific and must reference what the passage actually says.**
Good:  "Passage [S7] describes Databricks' deletion vectors; the claim generalises this to all \
vector databases."
Bad:   "The cited passage does not support this claim."
A generic restatement like the second example is useless to the writer. If you cannot say \
concretely what the mismatch is, the claim is probably fine — do not flag it.

Respond with ONLY this JSON:
{
  "approved": false,
  "issues": [
    {"claim": "the exact sentence from the draft",
     "problem": "what is wrong with it",
     "severity": "major"}
  ]
}

If every claim is properly supported, return {"approved": true, "issues": []}."""


class Critic(Agent):
    name = "critic"
    system_prompt = SYSTEM_PROMPT
    temperature = 0.1
    # The critic emits a short JSON list of issues, so it does not need the default output
    # allowance. Keeping the whole call small matters: on a free tier this prompt already
    # carries the draft plus every cited passage.
    max_tokens = 1200
    # Quote length is the critic's whole basis for judgement. Trimming it to fit a rate limit
    # made the agent degrade into rubber-stamping every claim "not supported" with identical
    # boilerplate — worse than no critique, because it destroys trust in the reviewer note.
    # Keep quotes long and bound the prompt by batching sources instead.
    QUOTE_CHARS = 420
    SOURCES_PER_BATCH = 14

    def check_citations_mechanically(
        self, draft: str, state: ResearchState
    ) -> list[CriticIssue]:
        """Catch citations that point at nothing. Deterministic, no model involved."""
        issues: list[CriticIssue] = []
        for source_id in sorted(set(CITATION_RE.findall(draft))):
            if not state.registry.exists(source_id):
                issues.append(
                    CriticIssue(
                        claim=f"[{source_id}]",
                        problem=(
                            f"Citation [{source_id}] does not exist. Valid ids are "
                            f"S1-S{len(state.registry)}. Remove the claim or cite a real source."
                        ),
                        severity="major",
                    )
                )
        return issues

    @staticmethod
    def _claims_with_citations(draft: str) -> list[str]:
        """Split the draft into sentences, keeping only those that assert a cited fact.

        Headings, prose without citations, and the Limitations section carry no ``[S#]`` and
        are therefore skipped — which is also the behaviour we want, since flagging them was
        never the critic's job.
        """
        claims: list[str] = []
        for line in draft.splitlines():
            # Stop at the appended Sources list and reviewer note. Their entries contain
            # [S#] too, and without this guard the critic solemnly fact-checks its own
            # bibliography against itself.
            if _TRAILING_SECTION_RE.match(line.strip()):
                break

            stripped = line.strip().lstrip("-*# ").strip()
            if not stripped or "[S" not in stripped:
                continue
            if _SOURCE_ENTRY_RE.match(stripped) or stripped.startswith(">"):
                continue

            for sentence in re.split(r"(?<=[.!?])\s+", stripped):
                if CITATION_RE.search(sentence):
                    claims.append(sentence.strip())
        return claims

    def _batch_claims(
        self, claims: list[str], state: ResearchState
    ) -> list[tuple[list[str], set[str]]]:
        """Group claims so each batch references at most ``SOURCES_PER_BATCH`` sources."""
        batches: list[tuple[list[str], set[str]]] = []
        current: list[str] = []
        current_ids: set[str] = set()

        for claim in claims:
            ids = {i for i in CITATION_RE.findall(claim) if state.registry.exists(i)}
            if not ids:
                continue
            if current and len(current_ids | ids) > self.SOURCES_PER_BATCH:
                batches.append((current, current_ids))
                current, current_ids = [], set()
            current.append(claim)
            current_ids |= ids

        if current:
            batches.append((current, current_ids))
        return batches

    def run(self, state: ResearchState, draft: str) -> CriticVerdict:
        self.emit("verifying citations")

        mechanical = self.check_citations_mechanically(draft, state)
        if mechanical:
            self.emit(f"{len(mechanical)} invalid citation ids")

        # Pair each claim with only the passages it cites, then check those pairs in batches.
        #
        # The obvious implementations both fail on a free tier: one call carrying every
        # passage at full length exceeds the token ceiling, and shortening the passages
        # leaves the critic unable to judge, so it rubber-stamps everything as unsupported.
        # Re-sending the whole draft with each batch is worse still — it multiplies total
        # tokens by the batch count. Sending only the relevant sentences keeps every call
        # small in both dimensions and gives the critic exactly the claim/evidence pair it
        # needs to rule on.
        claims = self._claims_with_citations(draft)
        batches = self._batch_claims(claims, state)

        issues: list[CriticIssue] = list(mechanical)
        for batch_index, (batch_claims, batch_ids) in enumerate(batches, start=1):
            sources_block = "\n".join(
                f"[{s.id}] {s.url}\n     \"{s.quote[: self.QUOTE_CHARS].strip()}\""
                for s in state.registry.all()
                if s.id in batch_ids
            )
            if not sources_block or not batch_claims:
                continue

            # A batched critique is one graph node making many calls, so the graph's
            # between-node deadline check cannot see it. Check here too, and degrade to a
            # partial critique rather than blowing the run budget.
            if runtime.expired():
                self.emit(f"run deadline reached — checked {batch_index - 1}/{len(batches)} batches")
                state.note(
                    f"critic: stopped after {batch_index - 1}/{len(batches)} batches "
                    f"(run deadline); remaining claims unverified"
                )
                break

            if len(batches) > 1:
                self.emit(f"batch {batch_index}/{len(batches)} ({len(batch_claims)} claims)")

            claims_block = "\n".join(f"- {c}" for c in batch_claims)
            prompt = (
                f"Research question: {state.question}\n\n"
                f"## Claims to check\n\n{claims_block}\n\n"
                f"## The passages those claims cite\n\n{sources_block}"
            )

            batch_verdict = self.run_json(
                prompt, CriticVerdict, CriticVerdict(approved=True), state=state
            )
            issues.extend(batch_verdict.issues)

        verdict = CriticVerdict(approved=True, issues=issues)
        verdict.approved = not verdict.major_issues

        if verdict.approved:
            self.emit("approved")
        else:
            self.emit(f"{len(verdict.major_issues)} major issues")
            # Surface *what* was flagged, not just how many. Without this the loop is a
            # black box: you cannot tell a well-calibrated critic from a broken one.
            for issue in verdict.major_issues:
                self.emit(f"  ✗ {issue.claim[:88]}")
                self.emit(f"    → {issue.problem[:88]}")
        return verdict

    @staticmethod
    def format_issues(verdict: CriticVerdict) -> str:
        return "\n".join(
            f"- ({i.severity}) {i.claim}\n  Problem: {i.problem}" for i in verdict.issues
        )
