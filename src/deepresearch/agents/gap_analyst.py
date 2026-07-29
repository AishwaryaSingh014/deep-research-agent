"""Gap Analyst: decide whether the evidence is good enough, or what to search next.

This is what makes the system *deep* rather than one-shot. After round one it asks which
sub-questions are still thin and issues targeted follow-up queries for round two.
"""

from __future__ import annotations

from ..models import Gap, GapReport, ResearchState
from .base import Agent

SYSTEM_PROMPT = """You audit the evidence gathered for a research question and decide whether \
another round of searching is warranted.

For each sub-question you see the findings collected so far.

Mark a sub-question as a gap when:
- it has no findings at all, or
- its findings are vague, off-topic, or all restate the same single point, or
- an obvious counter-perspective or key detail is clearly missing.

Do NOT mark a gap simply because more detail is theoretically possible — only when the \
current evidence would leave a careful reader unable to answer the sub-question.

For each gap, give 1-2 NEW keyword search queries. They must be meaningfully different from \
the queries already tried, not rewordings.

Respond with ONLY this JSON:
{
  "sufficient": false,
  "gaps": [
    {"sub_question_id": "Q2", "reason": "why the evidence falls short",
     "followup_queries": ["new keyword query"]}
  ]
}

If the evidence is adequate everywhere, return {"sufficient": true, "gaps": []}."""


class GapAnalyst(Agent):
    name = "gap_analyst"
    system_prompt = SYSTEM_PROMPT
    temperature = 0.2

    def run(self, state: ResearchState, tried_queries: set[str]) -> GapReport:
        self.emit("auditing evidence coverage")

        if not state.plan:
            return GapReport(sufficient=True)

        blocks: list[str] = []
        for sub in state.plan.sub_questions:
            findings = state.findings_for(sub.id)
            if findings:
                body = "\n".join(f"  - {f.claim} {f.source_ids}" for f in findings)
            else:
                body = "  (no findings)"
            blocks.append(f"[{sub.id}] {sub.question}\n{body}")

        prompt = (
            f"Main question: {state.question}\n\n"
            f"Evidence so far:\n\n" + "\n\n".join(blocks) + "\n\n"
            f"Queries already tried: {sorted(tried_queries)}"
        )

        report = self.run_json(prompt, GapReport, GapReport(sufficient=True), state=state)

        # Drop gaps that name unknown sub-questions or recycle a tried query.
        valid_ids = {s.id for s in state.plan.sub_questions}
        cleaned: list[Gap] = []
        for gap in report.gaps:
            if gap.sub_question_id not in valid_ids:
                continue
            fresh = [
                q for q in gap.followup_queries if q.strip() and q.strip().lower() not in tried_queries
            ][:2]
            if fresh:
                gap.followup_queries = fresh
                cleaned.append(gap)

        report.gaps = cleaned
        report.sufficient = report.sufficient or not cleaned
        self.emit("evidence sufficient" if report.sufficient else f"{len(cleaned)} gaps to fill")
        return report
