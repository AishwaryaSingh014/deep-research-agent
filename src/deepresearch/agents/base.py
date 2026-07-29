"""Shared agent scaffolding: prompt -> call -> parse -> validate -> retry once -> safe default.

Small free-tier models produce malformed JSON often enough that it must be a designed-for
case, not an exception path. The contract here is that ``run_json`` **never raises** for a
bad model response: it re-prompts once with the validation error attached, and if that also
fails it returns the caller's fallback and records a note. One flaky agent degrades the
report; it does not kill the run.
"""

from __future__ import annotations

import json
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from .. import llm

T = TypeVar("T", bound=BaseModel)


class Agent:
    """Base class. Subclasses set ``name``/``system_prompt`` and expose a ``run`` method."""

    name: str = "agent"
    system_prompt: str = ""
    temperature: float | None = None
    max_tokens: int | None = None

    def __init__(self, on_event=None) -> None:
        # on_event(agent_name, message) lets the CLI show live progress without the
        # agents knowing anything about rich/typer.
        self._on_event = on_event

    def emit(self, message: str) -> None:
        if self._on_event:
            self._on_event(self.name, message)

    # ----------------------------------------------------------------- #
    def run_json(
        self,
        user_prompt: str,
        schema: type[T],
        fallback: T,
        *,
        state=None,
    ) -> T:
        """Call the model and coerce the reply into ``schema``. Never raises."""
        messages: list[llm.Message] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        raw = ""
        for attempt in range(2):
            try:
                raw = llm.complete(
                    messages,
                    agent=self.name,
                    json_mode=True,
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                )
                return schema.model_validate(llm.parse_json_loose(raw))
            except llm.LLMUnavailable:
                raise  # provider exhaustion is fatal and must surface to the CLI
            except (ValueError, ValidationError) as exc:
                if attempt == 0:
                    messages.append({"role": "assistant", "content": raw})
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                f"That response was invalid: {exc}\n\n"
                                f"Reply with ONLY a JSON object matching this schema:\n"
                                f"{json.dumps(schema.model_json_schema(), indent=2)}"
                            ),
                        }
                    )
                    continue
                if state is not None:
                    state.note(f"{self.name}: fell back to default after invalid JSON ({exc})")
                self.emit("invalid JSON twice — using safe default")
                return fallback

        return fallback

    def run_text(self, user_prompt: str) -> str:
        """Free-form generation, for the Synthesizer's markdown."""
        return llm.complete(
            [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            agent=self.name,
            json_mode=False,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )
