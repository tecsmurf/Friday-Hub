"""
Friday Hub CLI entry point.

Exit codes: 0 on success, 1 when a task or command fails, 2 on usage errors.
"""

import asyncio
import logging
from typing import Optional

import typer
from rich import box
from rich.markup import escape
from rich.table import Table

from friday.display import (
    console,
    show_banner,
    show_doctor,
    show_error,
    show_history,
    show_info,
    show_result,
    show_status,
)

app = typer.Typer(
    name="friday",
    help="Friday - your personal AI assistant: appointments, reminders, todos, automations, and AI coding agents.",
    add_completion=False,
    no_args_is_help=True,
)


@app.callback()
def _main(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Log diagnostic details to stderr"),
):
    """Friday Hub - AI Orchestration Operating System."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    # Third-party HTTP/DB libraries are noisy at DEBUG.
    for noisy in ("httpx", "httpcore", "chromadb", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _run(coro):
    """Run an async function synchronously."""
    return asyncio.run(coro)


def _fail(message: str, code: int = 1):
    show_error(message)
    raise typer.Exit(code)


@app.command()
def run(
    task: Optional[list[str]] = typer.Argument(None, help="Task to send to Hermes"),
    agent: Optional[str] = typer.Option(None, "--agent", "-a", help="Force a specific agent (no fallback)"),
):
    """Execute a task through Hermes."""
    show_banner()
    prompt = " ".join(task or []).strip()
    if not prompt:
        _fail('No task given. Usage: friday run "describe the task"', code=2)

    async def _execute():
        from friday.brain.hermes import HermesBrain

        with console.status("[bright_blue]Hermes is thinking...[/]", spinner="dots"):
            brain = HermesBrain()
            await brain.initialize()

        show_info(f"Task: {prompt}")
        if agent:
            show_info(f"Forced agent: {agent}")

        with console.status("[bright_blue]Routing and executing...[/]", spinner="dots"):
            return await brain.think(prompt, forced_agent=agent)

    try:
        result = _run(_execute())
    except KeyboardInterrupt:
        _fail("Interrupted.", code=130)
    except Exception as exc:
        _fail(str(exc))

    show_result(result)
    if not result["success"]:
        raise typer.Exit(1)


@app.command()
def status():
    """Show health status for model providers and agents."""
    show_banner()

    async def _status():
        from friday.brain.hermes import HermesBrain

        with console.status("[bright_blue]Checking system...[/]", spinner="dots"):
            return await HermesBrain().get_status()

    try:
        show_status(_run(_status()))
    except Exception as exc:
        _fail(str(exc))


@app.command()
def doctor():
    """Run full diagnostics (version guard smoke tests + health checks)."""
    show_banner()

    async def _doctor():
        from friday.brain.hermes import HermesBrain

        with console.status("[bright_blue]Running diagnostics...[/]", spinner="dots"):
            return await HermesBrain().get_status()

    try:
        system_status = _run(_doctor())
    except Exception as exc:
        _fail(str(exc))

    show_doctor(system_status)
    agents = system_status.get("agents", {})
    if not any(d["status"].installed and d["health"].get("healthy") for d in agents.values()):
        show_error("No healthy agents found.")
        raise typer.Exit(1)


@app.command()
def history(count: int = typer.Option(10, "--count", "-n", min=1, help="Number of entries")):
    """Show recent task history."""
    show_banner()

    from friday.memory.logger import get_recent

    show_history(get_recent(count))


@app.command()
def memory(query: str = typer.Argument(..., help="Search query for long-term memory")):
    """Search long-term memory for past tasks."""
    show_banner()

    from friday.memory import long_term

    try:
        results = long_term.search(query, tasks_only=True)
    except Exception as exc:
        _fail(f"Long-term memory is unavailable: {exc}")

    if not results:
        show_info("No matching memories found.")
        return

    table = Table(
        title="[bold bright_cyan]Memory Search Results[/]",
        box=box.ROUNDED,
        border_style="bright_blue",
    )
    table.add_column("ID", style="dim", max_width=28)
    table.add_column("Content", ratio=3)
    table.add_column("Agent", style="bright_cyan")
    table.add_column("Distance", justify="right", style="dim")

    for result in results:
        document = (result["document"] or "")[:80]
        agent = (result.get("metadata") or {}).get("agent", "?")
        distance = f"{result['distance']:.3f}" if result.get("distance") is not None else "-"
        table.add_row(escape(result["id"]), escape(document), escape(str(agent)), distance)

    console.print(table)


@app.command()
def feedback(
    task_id: str = typer.Argument(..., help="Task ID from `friday history`"),
    good: bool = typer.Option(..., "--good/--bad", help="Was the result good?"),
    comment: str = typer.Option("", "--comment", "-c", help="Optional note"),
):
    """Rate a past task so future routing can learn from it."""
    show_banner()

    from friday.learning.feedback import submit_feedback
    from friday.memory.logger import add_feedback

    if not add_feedback(task_id, "good" if good else "bad", comment):
        _fail(f"No task log found for '{task_id}'. See `friday history` for IDs.")
    try:
        submit_feedback(task_id, thumbs_up=good, comment=comment)
    except Exception as exc:
        _fail(f"Saved to the task log, but long-term memory is unavailable: {exc}")
    show_info(f"Recorded {'positive' if good else 'negative'} feedback for {task_id}.")


@app.command(name="config")
def show_config():
    """Show current configuration."""
    show_banner()

    from friday.config import CONFIG_PATH, ROOT_DIR, config

    console.print("[bold bright_cyan]Configuration[/]\n")
    console.print(f"  [cyan]Config Path:[/] {escape(str(CONFIG_PATH))}")
    console.print(f"  [cyan]Runtime Home:[/] {escape(str(ROOT_DIR))}")

    models = config.get("models", {})
    console.print(f"  [cyan]Primary Provider:[/] {escape(str(models.get('primary_provider', '?')))}")
    console.print(f"  [cyan]Fallback Provider:[/] {escape(str(models.get('fallback_provider', '?')))}")

    brain = models.get("brain", {})
    fast = models.get("fast", {})
    console.print(f"  [cyan]Brain Model (Ollama):[/] {escape(str(brain.get('ollama', '?')))}")
    console.print(f"  [cyan]Fast Model (Ollama):[/] {escape(str(fast.get('ollama', '?')))}")
    console.print()

    agents = config.get("agents", {})
    console.print("  [cyan]Agents:[/]")
    for agent_id, agent_config in agents.items():
        enabled = "[green]enabled[/]" if agent_config.get("enabled") else "[red]disabled[/]"
        name = escape(str(agent_config.get("name", agent_id)))
        priority = agent_config.get("priority", "?")
        console.print(f"    {name:15s} {enabled}  priority: {priority}")
    console.print()


from friday.assistant.cli import register as _register_assistant  # noqa: E402

_register_assistant(app)


def main():
    app()
