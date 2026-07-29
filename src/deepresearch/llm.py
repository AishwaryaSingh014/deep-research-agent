"""Provider-agnostic LLM access with retry, cross-provider fallback, and token accounting.

Design notes
------------
Free tiers rate-limit aggressively and small models emit malformed JSON. Both are treated
as *expected* conditions rather than exceptions:

  * before dispatch      -> a per-model token-bucket paces calls under the tokens-per-minute
    ceiling, so most 429s never happen in the first place.
  * on a 429             -> sleep for the window the provider itself reported, then walk the
    fallback chain. The chain runs Groq's models first and only then switches provider,
    because Groq meters per model: a different model is cheaper and likelier to answer than
    a different vendor.
  * permanently rejected -> a ``limit: 0`` quota is provisioning, not congestion; fail over
    immediately rather than retrying something that cannot recover.
  * malformed JSON       -> handled one layer up in ``agents/base.py``.

Every call carries a wall-clock deadline. Without one, obeying provider retry hints across a
four-deep chain can strand a single call for ~15 minutes.

Everything is synchronous; concurrency is the orchestrator's problem, not this module's.
"""

from __future__ import annotations

import json
import random
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from . import config

Message = dict[str, str]


class LLMUnavailable(RuntimeError):
    """Raised only when every configured provider has been exhausted."""


# --------------------------------------------------------------------------- #
# Usage ledger
# --------------------------------------------------------------------------- #
@dataclass
class CallRecord:
    agent: str
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_s: float
    attempts: int
    failed_over: bool


@dataclass
class UsageLedger:
    """Thread-safe record of every LLM call in a run. Surfaced by ``--verbose``."""

    records: list[CallRecord] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, record: CallRecord) -> None:
        with self._lock:
            self.records.append(record)

    def reset(self) -> None:
        with self._lock:
            self.records.clear()

    @property
    def total_calls(self) -> int:
        return len(self.records)

    @property
    def total_tokens(self) -> int:
        return sum(r.prompt_tokens + r.completion_tokens for r in self.records)

    def by_agent(self) -> dict[str, dict[str, int | float]]:
        out: dict[str, dict[str, int | float]] = {}
        for r in self.records:
            bucket = out.setdefault(
                r.agent, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "latency_s": 0.0}
            )
            bucket["calls"] += 1
            bucket["prompt_tokens"] += r.prompt_tokens
            bucket["completion_tokens"] += r.completion_tokens
            bucket["latency_s"] += r.latency_s
        return out

    def failover_count(self) -> int:
        return sum(1 for r in self.records if r.failed_over)


LEDGER = UsageLedger()


# --------------------------------------------------------------------------- #
# Proactive pacing
# --------------------------------------------------------------------------- #
class RateLimiter:
    """Rolling-window token budget, applied *before* a call is dispatched.

    Backoff is a reaction to being rejected; this is an attempt not to be rejected. A research
    run bursts well past a free tier's tokens-per-minute ceiling, so 429s are the expected
    outcome rather than an anomaly. Pacing converts them into short, predictable pauses.

    The reservation is an estimate (prompt chars / 4 + max output). ``settle`` corrects it
    once the provider reports real usage, so systematic error does not accumulate.
    """

    WINDOW_SECONDS = 60.0

    def __init__(self, tokens_per_minute: int) -> None:
        self.budget = tokens_per_minute
        self._events: deque[list[float]] = deque()  # [timestamp, tokens]
        self._lock = threading.Lock()
        self.pause_count = 0
        self.paused_seconds = 0.0

    def _prune(self, now: float) -> None:
        while self._events and now - self._events[0][0] > self.WINDOW_SECONDS:
            self._events.popleft()

    def try_reserve(self, estimated_tokens: int) -> list[float] | None:
        """Reserve without waiting. Returns ``None`` when the window is full.

        Callers use this to shop the fallback chain: a model whose minute is full should be
        skipped in favour of one that is free, not waited on. Sleeping here would leave the
        rest of the chain idle, which defeats the point of having it.
        """
        with self._lock:
            now = time.monotonic()
            self._prune(now)
            used = sum(event[1] for event in self._events)

            # Always admit when the window is empty, else an oversized single call would
            # deadlock against its own budget.
            if not self._events or used + estimated_tokens <= self.budget:
                entry = [now, float(estimated_tokens)]
                self._events.append(entry)
                return entry
            return None

    def reserve(self, estimated_tokens: int) -> list[float]:
        """Block until ``estimated_tokens`` fits. Last resort, once no model is free."""
        while True:
            entry = self.try_reserve(estimated_tokens)
            if entry is not None:
                return entry

            with self._lock:
                oldest = self._events[0][0] if self._events else time.monotonic()
                wait = self.WINDOW_SECONDS - (time.monotonic() - oldest)

            self.pause_count += 1
            self.paused_seconds += max(wait, 0.0)
            time.sleep(min(max(wait, 0.0), self.WINDOW_SECONDS) + 0.05)

    def settle(self, entry: list[float], actual_tokens: int) -> None:
        """Replace the estimate with the provider's reported usage."""
        if actual_tokens <= 0:
            return
        with self._lock:
            entry[1] = float(actual_tokens)

    def release(self, entry: list[float]) -> None:
        """Drop a reservation whose call failed, so a failure does not consume budget."""
        with self._lock:
            try:
                self._events.remove(entry)
            except ValueError:
                pass

    def reset(self) -> None:
        with self._lock:
            self._events.clear()
        self.pause_count = 0
        self.paused_seconds = 0.0


# One limiter PER MODEL, not one globally. Groq meters tokens per model, so a single shared
# budget would throttle the whole chain down to one model's allowance and waste the headroom
# that having a fallback chain exists to provide.
_LIMITERS: dict[str, RateLimiter] = {}
_LIMITERS_LOCK = threading.Lock()


def limiter_for(provider_name: str) -> RateLimiter:
    with _LIMITERS_LOCK:
        if provider_name not in _LIMITERS:
            _LIMITERS[provider_name] = RateLimiter(config.TOKENS_PER_MINUTE_BUDGET)
        return _LIMITERS[provider_name]


def limiter_stats() -> tuple[int, float]:
    """Aggregate (pause_count, paused_seconds) across every model's limiter."""
    with _LIMITERS_LOCK:
        limiters = list(_LIMITERS.values())
    return (
        sum(limiter.pause_count for limiter in limiters),
        sum(limiter.paused_seconds for limiter in limiters),
    )


def reset_limiters() -> None:
    with _LIMITERS_LOCK:
        for limiter in _LIMITERS.values():
            limiter.reset()


def _estimate_tokens(messages: list[Message], max_tokens: int) -> int:
    """Estimate a call's token cost for pacing purposes.

    Reserving the *full* ``max_tokens`` badly over-charges the window: a Reader call permits
    2048 output tokens but typically emits ~300, so a 10k budget would admit only three calls
    a minute. Reserve a realistic slice instead and let ``settle`` correct it against the
    provider's reported usage — under-estimating is self-healing, over-estimating is not.
    """
    chars = sum(len(m.get("content", "")) for m in messages)
    expected_output = min(max_tokens, max(256, max_tokens // 4))
    return int(chars / 4) + expected_output


# --------------------------------------------------------------------------- #
# Provider adapters
# --------------------------------------------------------------------------- #
def _is_json_mode_failure(exc: Exception) -> bool:
    """True when a provider rejected the request because of constrained JSON decoding.

    Smaller models routinely fail Groq's ``response_format={"type": "json_object"}`` on
    longer prompts, returning a 400 ("Failed to generate JSON" / "Failed to validate JSON")
    rather than a 429. A 400 is correctly non-retryable, so before this was handled the whole
    fallback chain collapsed the instant the primary model was rate-limited — and because
    only the *last* error was reported, it looked like a quota problem for a long time.

    The request itself is fine; only the decoding constraint is unsupported. Retrying the same
    model in plain-text mode and parsing the reply leniently recovers almost all of these.
    """
    text = str(exc).lower()
    status = getattr(exc, "status_code", None)
    looks_like_400 = status == 400 or "400" in text or "invalid_request" in text
    return looks_like_400 and "json" in text


def _is_retryable(exc: Exception) -> bool:
    """Rate limits, timeouts, and 5xx are worth retrying; 400s are not."""
    # A quota of *zero* is a provisioning problem, not congestion: the project has no
    # free-tier allowance for this model and never will. Backing off cannot fix it, so
    # fail over to the next provider immediately instead of burning ~6s of sleep.
    if "limit: 0" in str(exc):
        return False

    name = type(exc).__name__.lower()
    if "ratelimit" in name or "timeout" in name or "connection" in name:
        return True
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if isinstance(status, int):
        return status == 429 or 500 <= status < 600
    text = str(exc).lower()
    return "429" in text or "rate limit" in text or "resource_exhausted" in text or "503" in text


class _GroqProvider:
    def __init__(self, model: str = config.GROQ_MODEL) -> None:
        from groq import Groq

        self.model = model
        self.name = f"groq:{model.split('/')[-1]}"
        self._client = Groq(api_key=config.GROQ_API_KEY, timeout=config.LLM_TIMEOUT_SECONDS)

    def complete(
        self, messages: list[Message], json_mode: bool, temperature: float, max_tokens: int
    ) -> tuple[str, int, int]:
        kwargs: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        resp = self._client.chat.completions.create(**kwargs)
        usage = resp.usage
        return (
            resp.choices[0].message.content or "",
            getattr(usage, "prompt_tokens", 0) or 0,
            getattr(usage, "completion_tokens", 0) or 0,
        )


class _GeminiProvider:
    def __init__(self, model: str = config.GEMINI_MODEL) -> None:
        from google import genai

        self.model = model
        self.name = "gemini"
        self._genai = genai
        # The client must be held as an attribute: a temporary genai.Client() is garbage
        # collected as soon as `.models` is bound, which closes its HTTP transport and
        # makes every request fail with "client has been closed".
        self._client = genai.Client(api_key=config.GEMINI_API_KEY)

    def complete(
        self, messages: list[Message], json_mode: bool, temperature: float, max_tokens: int
    ) -> tuple[str, int, int]:
        from google.genai import types

        # Gemini takes the system prompt out-of-band and only knows user/model roles.
        system_parts = [m["content"] for m in messages if m["role"] == "system"]
        contents = [
            types.Content(
                role="model" if m["role"] == "assistant" else "user",
                parts=[types.Part.from_text(text=m["content"])],
            )
            for m in messages
            if m["role"] != "system"
        ]
        cfg = types.GenerateContentConfig(
            temperature=temperature,
            max_output_tokens=max_tokens,
            system_instruction="\n\n".join(system_parts) or None,
            response_mime_type="application/json" if json_mode else "text/plain",
        )
        resp = self._client.models.generate_content(
            model=self.model, contents=contents, config=cfg
        )
        meta = getattr(resp, "usage_metadata", None)
        return (
            resp.text or "",
            getattr(meta, "prompt_token_count", 0) or 0,
            getattr(meta, "candidates_token_count", 0) or 0,
        )


_PROVIDER_CACHE: dict[str, object] = {}
_PROVIDER_LOCK = threading.Lock()


def _available_providers() -> list:
    """Instantiate the fallback chain once, in priority order.

    The chain walks *models* before it walks providers, because Groq's limits are per-model:
    when the 70b's token-per-minute window is exhausted, the other Groq models still have
    their own full allowance. Only once every Groq model is saturated is it worth paying the
    latency of switching provider entirely.
    """
    with _PROVIDER_LOCK:
        specs: list[tuple[str, type, str]] = []
        if config.GROQ_API_KEY:
            specs.extend((f"groq:{m}", _GroqProvider, m) for m in config.GROQ_MODELS)
        if config.GEMINI_API_KEY:
            specs.append(("gemini", _GeminiProvider, config.GEMINI_MODEL))

        providers = []
        for key, cls, model in specs:
            if key not in _PROVIDER_CACHE:
                try:
                    _PROVIDER_CACHE[key] = cls(model)
                except Exception:  # noqa: BLE001 - a broken SDK must not kill the run
                    continue
            if key in _PROVIDER_CACHE:
                providers.append(_PROVIDER_CACHE[key])
        return providers


# Each provider words its retry hint differently, so match all the observed forms:
#   Gemini: 'Please retry in 37.4s'   and   '"retryDelay": "46s"'
#   Groq:   'Rate limit reached. Please try again in 8.5s'
#   Groq also uses compound durations for longer waits: 'try again in 2m30s', 'in 1m6.599s'.
# This one is matched first and carries an optional minutes group.
_RETRY_DURATION_RE = re.compile(
    r"(?:retry|try\s+again)\s+(?:in|after)\s+(?:(\d+)\s*m)?\s*(\d+(?:\.\d+)?)\s*s",
    re.IGNORECASE,
)
_RETRY_HINT_PATTERNS = (
    re.compile(r"['\"]retry[_-]?delay['\"]\s*:\s*['\"]?(\d+(?:\.\d+)?)", re.IGNORECASE),
    re.compile(r"retry[-_\s]?after\D{0,4}(\d+(?:\.\d+)?)", re.IGNORECASE),
)


def _retry_after_seconds(exc: Exception) -> float | None:
    """Extract the provider's own retry hint, if it gave one.

    Both Groq and Gemini report how long to wait on a 429. Ignoring that and using a generic
    2s/4s backoff means the retries are guaranteed to fail — the provider already told us the
    window is 37 seconds wide. Obeying the hint is the difference between a run that survives
    a rate limit and one that dies on it.
    """
    header_value = None
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is not None:
        try:
            header_value = headers.get("retry-after") or headers.get("Retry-After")
        except Exception:  # noqa: BLE001
            header_value = None
    if header_value:
        try:
            return float(header_value)
        except (TypeError, ValueError):
            pass

    text = str(exc)

    duration = _RETRY_DURATION_RE.search(text)
    if duration:
        minutes = float(duration.group(1)) if duration.group(1) else 0.0
        return minutes * 60.0 + float(duration.group(2))

    for pattern in _RETRY_HINT_PATTERNS:
        match = pattern.search(text)
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                continue
    return None


def _backoff_delay(attempt: int, exc: Exception | None = None) -> float:
    hinted = _retry_after_seconds(exc) if exc is not None else None
    if hinted is not None:
        delay = min(hinted, config.BACKOFF_HINT_MAX_SECONDS)
    else:
        delay = min(config.BACKOFF_BASE_SECONDS * (2**attempt), config.BACKOFF_MAX_SECONDS)
    return delay + random.uniform(0, 0.5)  # jitter avoids thundering-herd on fan-out


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def complete(
    messages: list[Message],
    *,
    agent: str = "unknown",
    json_mode: bool = False,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> str:
    """Return model text, trying every provider before giving up.

    Raises ``LLMUnavailable`` only when all providers are exhausted.
    """
    providers = _available_providers()
    if not providers:
        raise LLMUnavailable(
            "No LLM provider configured. Set GROQ_API_KEY and/or GEMINI_API_KEY in .env "
            "(see .env.example)."
        )

    temperature = config.DEFAULT_TEMPERATURE if temperature is None else temperature
    max_tokens = config.MAX_OUTPUT_TOKENS if max_tokens is None else max_tokens

    last_error: Exception | None = None
    # Per-provider errors. Reporting only the *last* one is useless with a fallback chain:
    # the last provider is Gemini, so every failure looked like a Gemini quota problem even
    # when the real cause was upstream on Groq.
    errors: dict[str, str] = {}
    total_attempts = 0
    # One deadline for the whole call, not per attempt. Obeying provider retry hints without
    # this lets a single call sleep for ~15 minutes across the full chain.
    deadline = time.monotonic() + config.LLM_CALL_DEADLINE_SECONDS
    estimate = _estimate_tokens(messages, max_tokens)

    # Two phases, and the order matters.
    #
    # PHASE 0 — sweep every model once, never sleeping. A 429 usually means *this* model is
    # busy this minute, not that the service is down, so the cheapest fix is another model.
    # PHASE 1 — only once the whole chain has refused: back off and retry patiently.
    #
    # Doing it the other way round (retry-then-failover) meant the first model consumed the
    # entire call deadline in backoff sleeps, and the remaining three were skipped without a
    # single attempt — exactly the opposite of what a fallback chain is for.
    for phase in (0, 1):
        patient = phase == 1

        for provider_index, provider in enumerate(providers):
            attempts_allowed = config.MAX_ATTEMPTS_PER_PROVIDER if patient else 1

            for attempt in range(attempts_allowed):
                if time.monotonic() >= deadline:
                    last_error = last_error or TimeoutError(
                        f"exceeded {config.LLM_CALL_DEADLINE_SECONDS:.0f}s call deadline"
                    )
                    return _fail(last_error, errors)

                limiter = limiter_for(provider.name)
                reservation = limiter.try_reserve(estimate)
                if reservation is None:
                    errors.setdefault(
                        provider.name, f"throttled locally (estimated {estimate} tok)"
                    )
                    if not patient:
                        break  # this model's minute is spent; another is probably free
                    reservation = limiter.reserve(estimate)

                total_attempts += 1
                started = time.monotonic()
                result = None
                try:
                    result = provider.complete(
                        messages, json_mode, temperature, max_tokens
                    )
                except Exception as exc:  # noqa: BLE001 - classified below
                    # Constrained JSON decoding is unsupported by some models on longer
                    # prompts. The request is fine; only the decoding mode is. Retry this
                    # same model in plain text and let parse_json_loose handle the reply.
                    if json_mode and _is_json_mode_failure(exc):
                        try:
                            result = provider.complete(
                                messages, False, temperature, max_tokens
                            )
                        except Exception as retry_exc:  # noqa: BLE001
                            exc = retry_exc

                    if result is None:
                        # A rejected call consumed no provider tokens; don't charge it.
                        limiter.release(reservation)
                        last_error = exc
                        errors[provider.name] = f"{type(exc).__name__}: {str(exc)[:160]}"

                        if not _is_retryable(exc):
                            break  # a 400 will not become a 200; move on
                        if not patient or attempt >= attempts_allowed - 1:
                            break

                        delay = _backoff_delay(attempt, exc)
                        if time.monotonic() + delay >= deadline:
                            break
                        time.sleep(delay)
                        continue

                text, prompt_tokens, completion_tokens = result
                errors.pop(provider.name, None)

                # Correct the reservation with real usage so estimation error cannot drift.
                limiter.settle(reservation, prompt_tokens + completion_tokens)

                LEDGER.add(
                    CallRecord(
                        agent=agent,
                        provider=provider.name,
                        model=provider.model,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        latency_s=time.monotonic() - started,
                        attempts=total_attempts,
                        failed_over=provider_index > 0 or phase > 0,
                    )
                )
                return text

    return _fail(last_error, errors)


def _fail(last_error: Exception | None, errors: dict[str, str] | None = None):
    detail = ""
    if errors:
        detail = "\n" + "\n".join(f"  {name}: {msg}" for name, msg in errors.items())
    raise LLMUnavailable(f"All providers failed.{detail}") from last_error


def complete_json(
    messages: list[Message],
    *,
    agent: str = "unknown",
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> dict:
    """``complete`` in JSON mode, tolerant of models that wrap output in prose or fences."""
    raw = complete(
        messages, agent=agent, json_mode=True, temperature=temperature, max_tokens=max_tokens
    )
    return parse_json_loose(raw)


def parse_json_loose(raw: str) -> dict:
    """Best-effort JSON extraction. Raises ``ValueError`` if nothing parses."""
    text = (raw or "").strip()
    if not text:
        raise ValueError("empty response")

    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text.lstrip("`")
        text = text.removeprefix("json").strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Fall back to the outermost {...} span — handles leading/trailing commentary.
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError(f"could not parse JSON: {exc}") from exc

    raise ValueError("no JSON object found in response")
