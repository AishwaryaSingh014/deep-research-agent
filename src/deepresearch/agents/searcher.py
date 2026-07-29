"""Searcher: run queries, dedupe hits, and pick the most promising URLs.

Deliberately **not** an LLM agent. Selecting which of ten search hits to read is a
similarity-ranking problem, and a local embedding model does it for free, instantly, and
deterministically. Spending an LLM call here would be slower, cost free-tier budget, and
produce a worse ordering. Use a model where judgement is needed; use maths where it is not.
"""

from __future__ import annotations

from .. import config
from ..tools import rank, search
from .base import Agent


class Searcher(Agent):
    name = "searcher"

    def run(
        self,
        sub_question: str,
        queries: list[str],
        seen_urls: set[str],
        limit: int = config.MAX_URLS_PER_SUBQUESTION,
    ) -> list[search.SearchResult]:
        self.emit(f"searching: {queries[0][:48]}")

        raw: list[search.SearchResult] = []
        for query in queries[:2]:
            raw.extend(search.search(query, limit=5))

        candidates = search.dedupe(raw, seen=seen_urls, max_per_domain=2)
        if not candidates:
            self.emit("no new results")
            return []

        if len(candidates) <= limit:
            return candidates

        # Rank by how well title+snippet matches the sub-question, then keep the best.
        blurbs = [f"{c.title}. {c.snippet}" for c in candidates]
        ranked = rank.top_k(sub_question, blurbs, k=limit)
        chosen_blurbs = {text for text, _ in ranked}
        selected = [c for c, b in zip(candidates, blurbs) if b in chosen_blurbs][:limit]

        self.emit(f"{len(selected)} sources selected")
        return selected
