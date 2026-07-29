"""Planner: turn one broad question into a small set of independently-researchable ones."""

from __future__ import annotations

from .. import config
from ..models import Plan, SubQuestion
from .base import Agent

SYSTEM_PROMPT = """You are a research planner. You decompose a question into sub-questions \
that can each be answered independently by searching the web.

Rules:
- Produce 3 to {max_sub} sub-questions. Fewer is better if the question is narrow.
- Each sub-question must be answerable from public web sources, and must be genuinely \
distinct from the others. Do not restate the main question.
- Cover the question's real dimensions (mechanism, trade-offs, alternatives, evidence, \
limitations), not just its surface wording.
- CRITICAL — no topic drift. Every sub-question must be about the SPECIFIC thing asked, not \
about its general subject area. For "how do vector databases handle deletes?", a sub-question \
about deletion mechanisms is correct; one about what vector databases are, or how similarity \
search works, is drift and will pull in useless sources. Each sub-question must contain the \
distinguishing concept of the original question, not just its broad domain.
- Give each sub-question 1-2 web search queries. Queries are keyword-style, not questions. \
Include the distinguishing concept in every query for the same reason.

Respond with ONLY this JSON:
{{
  "interpretation": "one sentence on how you read the question",
  "sub_questions": [
    {{"id": "Q1", "question": "...", "search_queries": ["...", "..."]}}
  ]
}}"""


class Planner(Agent):
    name = "planner"
    system_prompt = SYSTEM_PROMPT.format(max_sub=config.MAX_SUBQUESTIONS)
    temperature = 0.3

    def run(self, question: str, state=None) -> Plan:
        self.emit("decomposing the question")

        fallback = Plan(
            interpretation="Fell back to a single-question plan.",
            sub_questions=[SubQuestion(id="Q1", question=question, search_queries=[question])],
        )
        plan = self.run_json(
            f"Research question: {question}", Plan, fallback, state=state
        )

        # Normalise: enforce ids, cap count, drop empties, guarantee a query per sub-question.
        cleaned: list[SubQuestion] = []
        for index, sub in enumerate(plan.sub_questions[: config.MAX_SUBQUESTIONS], start=1):
            if not sub.question.strip():
                continue
            sub.id = f"Q{index}"
            sub.search_queries = [q for q in sub.search_queries if q.strip()][:2] or [
                sub.question
            ]
            cleaned.append(sub)

        plan.sub_questions = cleaned or fallback.sub_questions
        self.emit(f"{len(plan.sub_questions)} sub-questions")
        return plan
