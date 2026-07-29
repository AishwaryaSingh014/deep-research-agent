"""Reader: turn one web page into grounded findings for one sub-question.

This is the load-bearing agent. Two things make its output trustworthy:

1. **Retrieval, not stuffing.** A page is chunked and only the top-k passages relevant to
   the sub-question are shown to the model. A 50k-token page costs ~800 tokens to read.

2. **Quotes are verbatim by construction.** The model is shown passages labelled P1..Pk and
   must cite those labels. It never writes the quote itself, so it cannot fabricate one.
   Python then maps P-labels onto the run's [S#] source ids.
"""

from __future__ import annotations

from .. import config
from ..models import Finding, ReaderOutput, SourceRegistry
from ..tools import fetch, rank, search
from .base import Agent

SYSTEM_PROMPT = """You extract factual findings from a source document to answer a specific \
sub-question.

You are given numbered passages (P1, P2, ...) taken verbatim from one web page.

Rules:
- Extract only claims that the passages actually support. Do not add outside knowledge.
- Every finding must cite at least one passage id.
- If the passages do not address the sub-question, return an empty findings list and set \
"relevant" to false. Returning nothing is correct and expected — never invent a finding to \
seem useful.
- Write each claim as one self-contained sentence that would make sense on its own.
- Extract at most 4 findings. Prefer specific, concrete facts over vague summary.

Respond with ONLY this JSON:
{
  "relevant": true,
  "findings": [
    {"claim": "one self-contained factual sentence", "source_ids": ["P1"]}
  ]
}"""


class Reader(Agent):
    name = "reader"
    system_prompt = SYSTEM_PROMPT
    temperature = 0.1

    def run(
        self,
        sub_question_id: str,
        sub_question: str,
        result: search.SearchResult,
        registry: SourceRegistry,
        state=None,
    ) -> list[Finding]:
        # Tavily often returns page content inline, which saves a fetch entirely.
        text = result.content if len(result.content) > 500 else None
        if not text:
            text = fetch.fetch_text(result.url)
        if not text:
            self.emit(f"unreadable: {result.url[:44]}")
            return []

        chunks = rank.chunk_text(text)
        passages = rank.top_k(sub_question, chunks, k=config.TOP_K_PASSAGES)
        if not passages:
            return []

        labelled = {f"P{i}": passage for i, (passage, _score) in enumerate(passages, start=1)}
        block = "\n\n".join(f"[{label}] {passage}" for label, passage in labelled.items())

        prompt = (
            f"Sub-question: {sub_question}\n\n"
            f"Source: {result.title or result.url}\n"
            f"URL: {result.url}\n\n"
            f"Passages:\n{block}"
        )

        output = self.run_json(prompt, ReaderOutput, ReaderOutput(findings=[]), state=state)
        if not output.relevant or not output.findings:
            return []

        # Map P-labels to run-global [S#] ids, dropping any label the model invented.
        findings: list[Finding] = []
        for finding in output.findings[:4]:
            source_ids: list[str] = []
            for label in finding.source_ids:
                passage = labelled.get(label.strip().upper())
                if passage is None:
                    continue
                source = registry.add(
                    url=result.url,
                    title=result.title,
                    quote=passage,
                    sub_question_id=sub_question_id,
                )
                source_ids.append(source.id)

            if not source_ids or not finding.claim.strip():
                continue  # an uncitable finding is not a finding
            findings.append(
                Finding(
                    claim=finding.claim.strip(),
                    source_ids=sorted(set(source_ids)),
                    sub_question_id=sub_question_id,
                )
            )

        self.emit(f"{len(findings)} findings from {result.url[:40]}")
        return findings
