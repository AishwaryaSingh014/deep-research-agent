"""Web search with provider fallback, result dedupe, and a global budget.

Tavily is primary because it returns pre-extracted content alongside each hit, which often
saves a page fetch entirely. DuckDuckGo needs no API key at all, so the tool still works for
someone who clones the repo and only sets an LLM key.

``MAX_SEARCHES_TOTAL`` is enforced here rather than in the agents: a budget that lives next
to the thing it limits cannot be forgotten by a caller.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import asdict, dataclass
from urllib.parse import urlparse

from .. import config


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str
    # Tavily returns page content inline; when present the Reader can skip fetching.
    content: str = ""


class SearchBudget:
    """Hard cap on searches per run. Prevents a looping agent from burning the free tier."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0
        self._lock = threading.Lock()

    def try_consume(self) -> bool:
        with self._lock:
            if self.used >= self.limit:
                return False
            self.used += 1
            return True

    def reset(self) -> None:
        with self._lock:
            self.used = 0

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)


BUDGET = SearchBudget(config.MAX_SEARCHES_TOTAL)

_SEARCH_CACHE = config.CACHE_DIR / "searches"


def _cache_path(query: str, limit: int):
    digest = hashlib.sha256(f"{query}|{limit}".encode()).hexdigest()[:24]
    return _SEARCH_CACHE / f"{digest}.json"


def _read_cache(query: str, limit: int) -> list[SearchResult] | None:
    path = _cache_path(query, limit)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return [SearchResult(**item) for item in raw]
    except (json.JSONDecodeError, OSError, TypeError):
        return None


def _write_cache(query: str, limit: int, results: list[SearchResult]) -> None:
    try:
        _SEARCH_CACHE.mkdir(parents=True, exist_ok=True)
        _cache_path(query, limit).write_text(
            json.dumps([asdict(r) for r in results]), encoding="utf-8"
        )
    except OSError:
        pass


def _search_tavily(query: str, limit: int) -> list[SearchResult]:
    from tavily import TavilyClient

    client = TavilyClient(api_key=config.TAVILY_API_KEY)
    payload = client.search(query=query, max_results=limit, search_depth="basic")
    return [
        SearchResult(
            title=item.get("title", "") or "",
            url=item.get("url", "") or "",
            snippet=(item.get("content", "") or "")[:500],
            content=item.get("raw_content") or item.get("content", "") or "",
        )
        for item in payload.get("results", [])
        if item.get("url")
    ]


def _search_ddg(query: str, limit: int) -> list[SearchResult]:
    from ddgs import DDGS

    with DDGS() as ddgs:
        hits = ddgs.text(query, max_results=limit)
    return [
        SearchResult(
            title=item.get("title", "") or "",
            url=item.get("href") or item.get("url") or "",
            snippet=(item.get("body", "") or "")[:500],
        )
        for item in hits
        if item.get("href") or item.get("url")
    ]


def _normalize(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.netloc.lower().removeprefix('www.')}{parsed.path.rstrip('/')}"


def dedupe(
    results: list[SearchResult], seen: set[str] | None = None, max_per_domain: int = 2
) -> list[SearchResult]:
    """Drop repeat URLs and stop any single domain from dominating the evidence base."""
    seen = seen if seen is not None else set()
    per_domain: dict[str, int] = {}
    out: list[SearchResult] = []

    for result in results:
        key = _normalize(result.url)
        if not key or key in seen:
            continue
        domain = urlparse(result.url).netloc.lower()
        if per_domain.get(domain, 0) >= max_per_domain:
            continue
        seen.add(key)
        per_domain[domain] = per_domain.get(domain, 0) + 1
        out.append(result)

    return out


def search(query: str, limit: int = 5) -> list[SearchResult]:
    """Return search hits, trying Tavily then DuckDuckGo. Never raises."""
    cached = _read_cache(query, limit)
    if cached is not None:
        return cached

    if not BUDGET.try_consume():
        return []

    providers = []
    if config.TAVILY_API_KEY:
        providers.append(("tavily", _search_tavily))
    providers.append(("duckduckgo", _search_ddg))

    for _name, fn in providers:
        try:
            results = fn(query, limit)
        except Exception:  # noqa: BLE001 - try the next provider
            continue
        if results:
            _write_cache(query, limit, results)
            return results

    return []
