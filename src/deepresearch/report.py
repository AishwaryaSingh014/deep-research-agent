"""Report assembly. Pure functions over finished state — no orchestration, no LLM calls.

The one exception is ``save``, which writes a finished report to disk. It lives here rather
than in the CLI because every entry point needs it: a report that exists only in the memory
of whichever process happened to run it is lost on restart.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .models import CriticIssue, ResearchState


@dataclass
class RunReport:
    question: str
    markdown: str
    state: ResearchState
    elapsed_s: float = 0.0
    approved: bool = False
    outstanding_issues: list[CriticIssue] = field(default_factory=list)
    resumed: bool = False


def slugify(text: str, max_length: int = 60) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_length].rstrip("-") or "report"


def save(question: str, markdown: str, destination: Path | None = None) -> Path:
    """Write a finished report to ``outputs/<slug>.md`` and return the path.

    Overwrites deliberately: the slug is derived from the question, so re-running a question
    replaces its report rather than accumulating near-duplicates. That mirrors the checkpoint
    thread id, which is derived the same way.
    """
    path = destination or (config.OUTPUT_DIR / f"{slugify(question)}.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown, encoding="utf-8")
    return path


_MODEL_SOURCES_RE = re.compile(
    # Matches a markdown heading ("## Sources") *or* a bare label line ("Sources:"), since
    # models produce both and only stripping the heading form leaves a stray list behind.
    r"\n(?:#{1,6}[ \t]*(?:sources|references|citations)\b|(?:\*\*)?(?:sources|references|citations)(?:\*\*)?[ \t]*:[ \t]*)"
    r".*?(?=\n#{1,6}[ \t]|\Z)",
    re.IGNORECASE | re.DOTALL,
)


def strip_model_sources_section(draft: str) -> str:
    """Remove any Sources section the model wrote itself.

    The synthesizer is told not to write one, because the real list is appended from the
    registry with working links. It sometimes writes one anyway — and a prompt instruction
    is not an enforcement mechanism. Stripping it in code guarantees exactly one Sources
    section instead of hoping for one.
    """
    return _MODEL_SOURCES_RE.sub("\n", draft).rstrip()


def sources_section(state: ResearchState) -> str:
    if not len(state.registry):
        return ""
    lines = ["", "## Sources", ""]
    for source in state.registry.all():
        lines.append(f"- **[{source.id}]** [{source.title[:100]}]({source.url})")
    return "\n".join(lines)


def reviewer_note(issues: list[CriticIssue], revisions: int) -> str:
    """Disclose disputed claims rather than hiding them behind a count.

    An unverified claim the reader can see is far safer than one they cannot.
    """
    if not issues:
        return ""
    flagged = "\n".join(
        f"> - *\"{i.claim.strip()[:180]}\"*  \n>   {i.problem.strip()[:200]}" for i in issues
    )
    return (
        "\n\n---\n\n"
        f"> **Reviewer note:** after {revisions} revision round(s), the fact-checking agent "
        f"still disputes {len(issues)} claim(s) below. They are left in place, flagged, "
        "rather than silently removed.\n>\n"
        f"{flagged}\n"
    )


def insufficient_evidence_report(state: ResearchState) -> str:
    """Returned when research genuinely found nothing. Saying so beats inventing an answer."""
    attempted = (
        "\n".join(f"- {s.question}" for s in state.plan.sub_questions) if state.plan else ""
    )
    return (
        f"# {state.question}\n\n"
        "## Summary\n\n"
        "**Insufficient evidence.** The research process did not find sources that "
        "substantively address this question, so no answer is given here. Fabricating one "
        "would be worse than returning nothing.\n\n"
        "## Sub-questions attempted\n\n"
        f"{attempted}\n\n"
        "## Limitations\n\n"
        "Possible causes: the topic is too new or too niche for indexed sources, the "
        "phrasing did not match how sources discuss it, or the pages found were "
        "unreadable (paywalls, JavaScript-only rendering, fetch failures).\n"
    )
