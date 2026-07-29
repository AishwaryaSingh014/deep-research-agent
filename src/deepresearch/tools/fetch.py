"""Fetch a URL and reduce it to clean article text.

The web is hostile: pages 404, paywall, hang, or return 40MB of JavaScript. Every failure
mode here is caught and reported as ``None`` so one bad URL can never abort a research run.
Successful fetches are cached to disk, which makes re-runs fast and free.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass

import httpx

from .. import config


@dataclass
class FetchStats:
    hits: int = 0
    misses: int = 0
    failures: int = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0


STATS = FetchStats()
_STATS_LOCK = threading.Lock()

_PAGE_CACHE = config.CACHE_DIR / "pages"

# Cheap pre-filter: never waste a fetch on a binary we cannot read.
_SKIP_SUFFIXES = (".pdf", ".zip", ".mp4", ".mp3", ".png", ".jpg", ".jpeg", ".gif", ".exe")


def _cache_path(url: str):
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
    return _PAGE_CACHE / f"{digest}.json"


def _read_cache(url: str) -> str | None:
    path = _cache_path(url)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("text")
    except (json.JSONDecodeError, OSError):
        return None


def _write_cache(url: str, text: str) -> None:
    try:
        _PAGE_CACHE.mkdir(parents=True, exist_ok=True)
        _cache_path(url).write_text(
            json.dumps({"url": url, "text": text}), encoding="utf-8"
        )
    except OSError:
        pass  # a read-only cache dir is not worth failing a run over


def fetch_text(url: str) -> str | None:
    """Return extracted article text, or ``None`` if the page is unusable.

    Never raises. Callers treat ``None`` as "skip this source".
    """
    if any(url.lower().split("?")[0].endswith(s) for s in _SKIP_SUFFIXES):
        return None

    cached = _read_cache(url)
    if cached is not None:
        with _STATS_LOCK:
            STATS.hits += 1
        return cached or None

    with _STATS_LOCK:
        STATS.misses += 1

    try:
        response = httpx.get(
            url,
            timeout=config.FETCH_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers={"User-Agent": config.USER_AGENT, "Accept": "text/html,*/*"},
        )
        response.raise_for_status()

        content_type = response.headers.get("content-type", "")
        if "html" not in content_type and "text" not in content_type:
            return None

        html = response.text[: config.MAX_PAGE_CHARS]
    except Exception:  # noqa: BLE001 - timeouts, DNS, 403, TLS: all mean "skip"
        with _STATS_LOCK:
            STATS.failures += 1
        return None

    try:
        import trafilatura

        text = trafilatura.extract(
            html, include_comments=False, include_tables=True, no_fallback=False
        )
    except Exception:  # noqa: BLE001
        text = None

    if not text or len(text.strip()) < 200:
        # Too little signal to be worth an LLM call.
        _write_cache(url, "")
        return None

    text = text.strip()
    _write_cache(url, text)
    return text
