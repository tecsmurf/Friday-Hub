"""
Rich terminal display helpers for Friday Hub.
"""

import sys

if sys.platform == "win32":
    for _stream_name in ("stdout", "stderr"):
        _stream = getattr(sys, _stream_name, None)
        if _stream and hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console(force_terminal=True)


BANNER = """[bold bright_blue]
  +-------------------------------------------+
  |  FRIDAY HUB v2                            |
  |  AI Orchestration Operating System        |
  +-------------------------------------------+[/]"""


def show_banner():
    console.print(BANNER)
    console.print()


def show_status(system_status: dict):
    """Display model provider and agent health status."""
    models = system_status.get("models", {})
    model_table = Table(
        title="[bold bright_cyan]Model Providers[/]",
        box=box.ROUNDED,
        border_style="bright_blue",
        pad_edge=False,
    )
    model_table.add_column("Provider", style="white bold")
    model_table.add_column("Type", style="dim")
    model_table.add_column("Status", justify="center")

    for provider, info in models.items():
        status = "[bold green]Online[/]" if info["available"] else "[bold red]Offline[/]"
        model_table.add_row(provider.title(), info["type"], status)

    console.print(model_table)
    console.print()

    agents = system_status.get("agents", {})
    agent_table = Table(
        title="[bold bright_cyan]Agent Status[/]",
        box=box.ROUNDED,
        border_style="bright_blue",
        pad_edge=False,
    )
    agent_table.add_column("Agent", style="white bold")
    agent_table.add_column("Version", style="dim")
    agent_table.add_column("Status", justify="center")
    agent_table.add_column("Guard", style="dim")

    for _aid, data in agents.items():
        status_obj = data["status"]
        health = data["health"]
        guard = data.get("guard", {})

        if status_obj.installed and status_obj.healthy:
            status = "[bold green]Healthy[/]"
        elif status_obj.installed:
            status = "[bold yellow]Warning[/]"
        else:
            status = "[bold red]Offline[/]"

        version = f"v{status_obj.version}" if status_obj.version else "-"
        guard_status = guard.get("status", "-")
        message = health.get("message") if isinstance(health, dict) else None
        if message and status_obj.error:
            guard_status = status_obj.error[:40]

        agent_table.add_row(status_obj.name, version, status, guard_status)

    console.print(agent_table)
    console.print()

    memory = system_status.get("memory", {})
    console.print(
        f"  [dim]Memory:[/] "
        f"[cyan]{memory.get('short_term', 0)}[/] short-term  "
        f"[cyan]{memory.get('long_term', 0)}[/] long-term entries"
    )
    console.print()


def show_result(result: dict):
    """Display task execution result."""
    if result["success"]:
        routing = result.get("routing", {})
        agent_name = result.get("agent_name", result.get("agent", "?"))
        version = result.get("version", "?")
        duration = result.get("duration", 0)
        confidence = routing.get("confidence", 0) if routing else 0
        reason = routing.get("reason", "") if routing else ""
        fallback = result.get("fallback_from")

        header = Text()
        header.append("OK ", style="bold green")
        header.append(agent_name, style="bold bright_cyan")
        header.append(f"  v{version}", style="dim")
        header.append(f"  {duration}s", style="dim")
        if confidence:
            header.append(f"  confidence: {confidence:.0%}", style="dim")
        if fallback:
            header.append(f"  (fallback from {fallback})", style="yellow")

        console.print()
        console.print(header)
        if reason:
            console.print(f"  [dim italic]{reason}[/]")
        console.print()

        console.print(Panel(
            result["output"] or "[dim](no output)[/]",
            border_style="bright_blue",
            padding=(1, 2),
        ))
    else:
        console.print()
        console.print("  [bold red]Failed[/]")
        if result.get("error"):
            console.print(f"  [red]{result['error']}[/]")
        elif result.get("output"):
            console.print(f"  [red]{result['output']}[/]")
        console.print()


def show_history(entries: list[dict]):
    """Display task history."""
    if not entries:
        console.print("  [dim]No task history yet.[/]")
        return

    table = Table(
        title="[bold bright_cyan]Recent Tasks[/]",
        box=box.ROUNDED,
        border_style="bright_blue",
    )
    table.add_column("Status", justify="center", width=6)
    table.add_column("Agent", style="bright_cyan", min_width=12)
    table.add_column("Task", style="white", ratio=3)
    table.add_column("Duration", style="dim", justify="right")
    table.add_column("Time", style="dim")

    for entry in entries:
        status = "[green]OK[/]" if entry.get("success") else "[red]FAIL[/]"
        agent = entry.get("agent", "?")
        prompt = (entry.get("prompt", "?"))[:60]
        duration = f"{entry.get('duration_s', 0)}s"
        timestamp = entry.get("timestamp", "?")[:19]
        table.add_row(status, agent, prompt, duration, timestamp)

    console.print(table)


def show_doctor(system_status: dict):
    """Display full diagnostics."""
    console.print("[bold bright_cyan]Full Diagnostics[/]")
    console.print()
    show_status(system_status)

    agents = system_status.get("agents", {})
    for _aid, data in agents.items():
        guard = data.get("guard", {})
        status_obj = data["status"]
        if guard.get("status") == "updated":
            console.print(
                f"  [blue]Updated[/] [bold]{status_obj.name}[/]: "
                f"{guard.get('old_version')} -> {guard.get('new_version')}"
            )
        elif guard.get("status") == "incompatible":
            console.print(
                f"  [red]Incompatible[/] [bold]{status_obj.name}[/] after update. "
                f"Adapter needs fixing."
            )


def show_error(message: str):
    console.print(f"\n  [bold red]Error:[/] {message}\n")


def show_info(message: str):
    console.print(f"  [dim]{message}[/]")
