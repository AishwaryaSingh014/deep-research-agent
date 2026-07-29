"""Command-line entry point.

    python -m deepresearch.cli "your question" --verbose
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import config, graph, llm, report

app = typer.Typer(add_completion=False, help="Multi-agent deep research with verifiable citations.")
console = Console()


def _preflight() -> list[str]:
    """Check configuration before spending time on a run."""
    problems: list[str] = []
    if not config.GROQ_API_KEY and not config.GEMINI_API_KEY:
        problems.append(
            "No LLM key found. Set GROQ_API_KEY and/or GEMINI_API_KEY in .env "
            "(copy .env.example). Both have free tiers."
        )
    return problems


def _print_usage_table() -> None:
    ledger = llm.LEDGER
    if not ledger.records:
        return

    table = Table(title="LLM usage", title_style="bold", header_style="bold cyan")
    table.add_column("Agent")
    table.add_column("Calls", justify="right")
    table.add_column("Prompt tok", justify="right")
    table.add_column("Output tok", justify="right")
    table.add_column("Time", justify="right")

    for agent, stats in sorted(ledger.by_agent().items(), key=lambda kv: -kv[1]["calls"]):
        table.add_row(
            agent,
            str(stats["calls"]),
            f"{stats['prompt_tokens']:,}",
            f"{stats['completion_tokens']:,}",
            f"{stats['latency_s']:.1f}s",
        )
    table.add_section()
    table.add_row(
        "[bold]total[/bold]",
        f"[bold]{ledger.total_calls}[/bold]",
        "",
        f"[bold]{ledger.total_tokens:,} tok[/bold]",
        "",
    )
    console.print(table)

    if ledger.failover_count():
        console.print(
            f"[yellow]{ledger.failover_count()} call(s) failed over to the backup provider.[/yellow]"
        )


def _print_run_stats(run: report.RunReport) -> None:
    stats = graph.run_stats()
    table = Table(title="Run stats", title_style="bold", header_style="bold cyan")
    table.add_column("Metric")
    table.add_column("Value", justify="right")

    rows = [
        ("Research rounds", f"{run.state.rounds_run}/{config.MAX_RESEARCH_ROUNDS}"),
        ("Critic revisions", f"{run.state.critic_revisions}/{config.MAX_CRITIC_REVISIONS}"),
        ("Sub-questions", str(len(run.state.plan.sub_questions) if run.state.plan else 0)),
        ("Findings", str(len(run.state.findings))),
        ("Citable sources", str(len(run.state.registry))),
        ("Unique URLs", str(len(run.state.registry.unique_urls()))),
        ("Searches", f"{stats['searches_used']}/{stats['searches_limit']}"),
        ("Page cache hit rate", f"{stats['fetch_hit_rate']:.0%}"),
        ("Fetch failures", str(stats["fetch_failures"])),
        ("Embeddings", "ONNX" if stats["embeddings_active"] else "TF-IDF fallback"),
        ("Throttle pauses", f"{stats['throttle_pauses']} ({stats['throttle_seconds']:.0f}s)"),
        ("Resumed from checkpoint", "yes" if run.resumed else "no"),
        ("Elapsed", f"{run.elapsed_s:.1f}s"),
    ]
    for label, value in rows:
        table.add_row(label, value)
    console.print(table)

    if run.state.notes:
        console.print("\n[dim]Degradation notes:[/dim]")
        for note in run.state.notes:
            console.print(f"  [dim]- {note}[/dim]")


@app.command()
def main(
    question: str = typer.Argument(..., help="The research question."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show token and run statistics."),
    output: Path = typer.Option(
        None, "--output", "-o", help="Where to write the report (default: outputs/<slug>.md)."
    ),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress live progress."),
    fresh: bool = typer.Option(
        False,
        "--fresh",
        help="Ignore any saved checkpoint and start over. Re-running the same question "
        "resumes it by default; use this after editing prompts or agent code.",
    ),
) -> None:
    """Research QUESTION across the web and write a cited Markdown report."""
    problems = _preflight()
    if problems:
        for problem in problems:
            console.print(f"[bold red]Configuration error:[/bold red] {problem}")
        raise typer.Exit(code=1)

    console.print(Panel(question, title="[bold]Research question[/bold]", border_style="cyan"))

    def on_event(agent: str, message: str) -> None:
        if not quiet:
            console.print(f"  [dim]{agent:<13}[/dim] {message}")

    try:
        run = graph.run_research(question, on_event=on_event, fresh=fresh)
    except llm.LLMUnavailable as exc:
        console.print(f"\n[bold red]LLM unavailable:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
        raise typer.Exit(code=130) from None

    destination = report.save(question, run.markdown, destination=output)

    status = (
        "[green]citations verified[/green]"
        if run.approved
        else "[yellow]unresolved citation issues — see note in report[/yellow]"
    )
    console.print(f"\n{status}")
    console.print(f"[bold]Report written to[/bold] {destination}")

    if verbose:
        console.print()
        _print_usage_table()
        _print_run_stats(run)


if __name__ == "__main__":
    try:
        app()
    except KeyboardInterrupt:
        sys.exit(130)
