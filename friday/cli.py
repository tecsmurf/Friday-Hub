"""
Friday Hub CLI entry point.
"""

import asyncio
from typing import Optional

import typer
from rich import box
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
    help="Friday Hub - AI Orchestration Operating System",
    add_completion=False,
    no_args_is_help=True,
)


def _run(coro):
    """Run an async function synchronously."""
    return asyncio.run(coro)


@app.command()
def run(
    task: Optional[list[str]] = typer.Argument(None, help="Task to send to Hermes"),
    agent: Optional[str] = typer.Option(None, "--agent", "-a", help="Force a specific agent"),
):
    """Execute a task through Hermes."""
    if not task:
        show_banner()
        raise typer.Exit()

    prompt = " ".join(task)
    show_banner()

    async def _execute():
        from friday.brain.hermes import HermesBrain

        with console.status("[bright_blue]Hermes is thinking...[/]", spinner="dots"):
            brain = HermesBrain()
            await brain.initialize()

        show_info(f"Task: {prompt}")
        if agent:
            show_info(f"Forced agent: {agent}")

        with console.status("[bright_blue]Routing and executing...[/]", spinner="dots"):
            result = await brain.think(prompt, forced_agent=agent)

        show_result(result)

    try:
        _run(_execute())
    except Exception as exc:
        show_error(str(exc))


@app.command()
def status():
    """Show health status for model providers and agents."""
    show_banner()

    async def _status():
        from friday.brain.hermes import HermesBrain

        with console.status("[bright_blue]Checking system...[/]", spinner="dots"):
            brain = HermesBrain()
            system_status = await brain.get_status()

        show_status(system_status)

    try:
        _run(_status())
    except Exception as exc:
        show_error(str(exc))


@app.command()
def doctor():
    """Run full diagnostics."""
    show_banner()

    async def _doctor():
        from friday.brain.hermes import HermesBrain

        with console.status("[bright_blue]Running diagnostics...[/]", spinner="dots"):
            brain = HermesBrain()
            system_status = await brain.get_status()

        show_doctor(system_status)

    try:
        _run(_doctor())
    except Exception as exc:
        show_error(str(exc))


@app.command()
def history(count: int = typer.Option(10, "--count", "-n", help="Number of entries")):
    """Show recent task history."""
    show_banner()

    from friday.memory.logger import get_recent

    entries = get_recent(count)
    show_history(entries)


@app.command()
def memory(query: str = typer.Argument(..., help="Search query for long-term memory")):
    """Search long-term memory for past tasks."""
    show_banner()

    from friday.memory import long_term

    results = long_term.search(query)
    if not results:
        show_info("No matching memories found.")
        return

    table = Table(
        title="[bold bright_cyan]Memory Search Results[/]",
        box=box.ROUNDED,
        border_style="bright_blue",
    )
    table.add_column("ID", style="dim", max_width=20)
    table.add_column("Content", ratio=3)
    table.add_column("Agent", style="bright_cyan")
    table.add_column("Score", justify="right", style="dim")

    for result in results:
        document = (result["document"] or "")[:80]
        agent = result.get("metadata", {}).get("agent", "?")
        distance = f"{result.get('distance', 0):.3f}" if result.get("distance") is not None else "-"
        table.add_row(result["id"][:20], document, agent, distance)

    console.print(table)


@app.command(name="config")
def show_config():
    """Show current configuration."""
    show_banner()

    from friday.config import CONFIG_PATH, ROOT_DIR, config

    console.print("[bold bright_cyan]Configuration[/]\n")
    console.print(f"  [cyan]Config Path:[/] {CONFIG_PATH}")
    console.print(f"  [cyan]Runtime Home:[/] {ROOT_DIR}")

    models = config.get("models", {})
    console.print(f"  [cyan]Primary Provider:[/] {models.get('primary_provider', '?')}")
    console.print(f"  [cyan]Fallback Provider:[/] {models.get('fallback_provider', '?')}")

    brain = models.get("brain", {})
    fast = models.get("fast", {})
    console.print(f"  [cyan]Brain Model (Ollama):[/] {brain.get('ollama', '?')}")
    console.print(f"  [cyan]Fast Model (Ollama):[/] {fast.get('ollama', '?')}")
    console.print()

    agents = config.get("agents", {})
    console.print("  [cyan]Agents:[/]")
    for agent_id, agent_config in agents.items():
        enabled = "[green]enabled[/]" if agent_config.get("enabled") else "[red]disabled[/]"
        name = agent_config.get("name", agent_id)
        priority = agent_config.get("priority", "?")
        console.print(f"    {name:15s} {enabled}  priority: {priority}")
    console.print()


def main():
    app()
