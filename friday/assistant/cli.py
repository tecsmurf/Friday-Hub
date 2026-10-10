"""
Personal-assistant CLI commands: ask, agenda, todo, remind, schedule,
automate, daemon, calendar.
"""

import asyncio
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import typer
from rich import box
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from friday.assistant.timeparse import TimeParseError, format_when, parse_datetime, parse_duration
from friday.display import console, show_error, show_info

todo_app = typer.Typer(help="Manage your to-do list.", no_args_is_help=True)
remind_app = typer.Typer(help="One-off and repeating reminders.", no_args_is_help=True)
automate_app = typer.Typer(help="Recurring automations (briefings, notifications, agent tasks).", no_args_is_help=True)
daemon_app = typer.Typer(help="Background daemon that delivers reminders and runs automations.", no_args_is_help=True)
calendar_app = typer.Typer(help="Calendar import/export.", no_args_is_help=True)


def _assistant():
    from friday.assistant.service import Assistant

    return Assistant()


def _fail(message: str, code: int = 1):
    show_error(message)
    raise typer.Exit(code)


def _when(text: str) -> datetime:
    try:
        return parse_datetime(text)
    except TimeParseError as exc:
        _fail(f"I couldn't understand the time: {exc}. Try e.g. 'tomorrow 3pm' or 'in 2 hours'.", code=2)


def _ok(message: str) -> None:
    console.print(Text("  ✓ ", style="bold green") + Text(message))


def _daemon_hint() -> None:
    show_info("Reminders pop up while `friday daemon run` is running (`friday daemon install` starts it at log-on).")


# ── Results of high-level actions (shared by commands and `ask`) ───────


def do_schedule(title, when, duration=None, location=None, remind_before=None, force=False):
    from friday.assistant.service import SchedulingConflict

    assistant = _assistant()
    try:
        booked = assistant.schedule(title, when, duration_minutes=duration, location=location,
                                    remind_before=remind_before, allow_overlap=force)
    except SchedulingConflict as exc:
        _fail(f"{exc} Use --force to double-book, or pick another time.")
    except ValueError as exc:
        _fail(str(exc), code=2)
    e = booked.event
    where = f" at {e.location}" if e.location else ""
    _ok(f"Booked #{e.id} {e.title}{where}: {format_when(e.start)}-{e.end:%H:%M}")
    if booked.reminder_at:
        show_info(f"Reminder set for {format_when(booked.reminder_at)}.")
        _daemon_hint()


def do_remind(text, when, repeat=None):
    try:
        reminder = _assistant().remind(text, when, repeat=repeat)
    except (ValueError, TimeParseError) as exc:
        _fail(str(exc), code=2)
    from friday.assistant.recurrence import describe

    extra = f", repeating {describe(reminder.rule)}" if reminder.rule else ""
    _ok(f"Reminder #{reminder.id} '{reminder.text}' at {format_when(reminder.due)}{extra}")
    _daemon_hint()


def do_todo(title, due=None, priority=2):
    try:
        todo = _assistant().add_todo(title, due=due, priority=priority)
    except ValueError as exc:
        _fail(str(exc), code=2)
    due_s = f" (due {format_when(todo.due)})" if todo.due else ""
    _ok(f"Added todo #{todo.id}: {todo.title}{due_s}")


def do_automate(schedule, action, payload=None, name=None):
    try:
        auto = _assistant().automate(schedule, action, payload, name=name)
    except (ValueError, TimeParseError) as exc:
        _fail(str(exc), code=2)
    _ok(f"Automation #{auto.id} '{auto.name}' runs {auto.schedule}; first run {format_when(auto.next_run)}")
    _daemon_hint()


def show_agenda(day: datetime | None = None):
    text = _assistant().briefing(day)
    head, _, body = text.partition("\n")
    console.print(f"[bold bright_cyan]{escape(head)}[/]")
    console.print(Text(body))


def show_todos(include_done=False):
    todos = _assistant().store.list_todos(include_done=include_done)
    if not todos:
        show_info("No todos. Add one with: friday todo add \"buy milk\" --due friday")
        return
    table = Table(box=box.ROUNDED, border_style="bright_blue", title="[bold bright_cyan]Todos[/]")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Todo")
    table.add_column("Due", style="cyan")
    table.add_column("Pri", justify="center")
    now = datetime.now()
    for t in todos:
        title = Text(t.title, style="strike dim" if t.done_at else "")
        due = format_when(t.due) if t.due else "-"
        if t.due and t.due < now and not t.done_at:
            due = f"[red]{escape(due)} (overdue)[/]"
        else:
            due = escape(due)
        table.add_row(str(t.id), title, due, {1: "[red]high[/]", 2: "", 3: "[dim]low[/]"}[t.priority])
    console.print(table)


# ── ask ────────────────────────────────────────────────────────────────


def ask(
    request: list[str] = typer.Argument(..., help='What you want, e.g. "remind me to call mom at 6pm"'),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show how Friday understood it without doing it"),
    no_llm: bool = typer.Option(False, "--no-llm", help="Only use built-in rules to understand the request"),
):
    """Tell Friday what you need in plain language."""
    from friday.assistant.intent import understand

    text = " ".join(request).strip()
    if not text:
        _fail("Tell me what you need.", code=2)
    intent = asyncio.run(understand(text, use_llm=not no_llm))

    if dry_run:
        details = {k: v for k, v in vars(intent).items() if v not in (None, "", {})}
        console.print(Text(f"  Understood as: {details}"))
        return

    if intent.kind == "schedule":
        if not intent.when:
            _fail("When should I book it? e.g. friday ask \"book dentist friday 3pm\"", code=2)
        do_schedule(intent.title, intent.when, intent.duration, intent.location)
    elif intent.kind == "remind":
        if not intent.when and not intent.repeat:
            _fail("When should I remind you? e.g. \"remind me to stretch in 30 minutes\"", code=2)
        do_remind(intent.title, intent.when or datetime.now(), repeat=intent.repeat)
    elif intent.kind == "todo":
        do_todo(intent.title, due=intent.when)
    elif intent.kind == "agenda":
        show_agenda(intent.when)
    elif intent.kind == "list_todos":
        show_todos()
    elif intent.kind == "automate":
        if not intent.repeat:
            _fail("How often? e.g. \"every weekday at 8am brief me\"", code=2)
        action = intent.action or "agent_task"
        do_automate(intent.repeat, action, None if action == "briefing" else intent.title,
                    name=intent.title if action != "agent_task" else None)
    else:
        from friday.cli import run as run_task

        run_task(task=[text], agent=None)


# ── agenda / schedule / cancel ─────────────────────────────────────────


def agenda(day: Optional[list[str]] = typer.Argument(None, help="Which day, e.g. tomorrow, friday, 2026-10-14")):
    """Show the day's appointments, reminders and todos."""
    show_agenda(_when(" ".join(day)) if day else None)


def schedule(
    title: str = typer.Argument(..., help="What the appointment is"),
    at: str = typer.Option(..., "--at", help='When, e.g. "friday 3pm"'),
    duration: str = typer.Option(None, "--for", help='Length, e.g. "45m" or "1h" (default from config)'),
    where: str = typer.Option(None, "--where", help="Location"),
    remind: Optional[int] = typer.Option(None, "--remind", help="Minutes before to remind (-1 for none)"),
    force: bool = typer.Option(False, "--force", help="Book even if it overlaps another appointment"),
):
    """Book an appointment in Friday's calendar (with a reminder)."""
    minutes = None
    if duration:
        try:
            minutes = parse_duration(duration)
        except TimeParseError as exc:
            _fail(str(exc), code=2)
    do_schedule(title, _when(at), minutes, where, remind_before=remind, force=force)


def cancel(event_id: int = typer.Argument(..., help="Appointment number from `friday agenda`")):
    """Cancel an appointment (and its pending reminder)."""
    if not _assistant().cancel(event_id):
        _fail(f"No active appointment #{event_id}.")
    _ok(f"Cancelled appointment #{event_id}.")


def free(
    duration: str = typer.Argument("1h", help='How long, e.g. "30m" or "2h"'),
    after: Optional[str] = typer.Option(None, "--after", help="Search from this time"),
):
    """Find the next free slot in working hours."""
    try:
        minutes = parse_duration(duration)
    except TimeParseError as exc:
        _fail(str(exc), code=2)
    slot = _assistant().find_free_slot(minutes, after=_when(after) if after else None)
    if not slot:
        _fail("No free slot in the next two weeks within working hours.")
    _ok(f"Next free {duration} slot: {format_when(slot)}")


# ── todo ───────────────────────────────────────────────────────────────


@todo_app.command("add")
def todo_add(
    title: list[str] = typer.Argument(..., help="The todo"),
    due: Optional[str] = typer.Option(None, "--due", help='Due date, e.g. "friday"'),
    high: bool = typer.Option(False, "--high", help="High priority"),
    low: bool = typer.Option(False, "--low", help="Low priority"),
):
    """Add a todo."""
    do_todo(" ".join(title), due=_when(due) if due else None, priority=1 if high else 3 if low else 2)


@todo_app.command("list")
def todo_list(all_: bool = typer.Option(False, "--all", help="Include completed")):
    """List todos."""
    show_todos(include_done=all_)


@todo_app.command("done")
def todo_done(todo_id: int = typer.Argument(..., help="Todo number")):
    """Mark a todo as done."""
    if not _assistant().store.complete_todo(todo_id):
        _fail(f"No open todo #{todo_id}.")
    _ok(f"Done: #{todo_id}")


@todo_app.command("rm")
def todo_rm(todo_id: int = typer.Argument(..., help="Todo number")):
    """Delete a todo."""
    if not _assistant().store.delete_todo(todo_id):
        _fail(f"No todo #{todo_id}.")
    _ok(f"Deleted todo #{todo_id}")


# ── remind ─────────────────────────────────────────────────────────────


@remind_app.command("add")
def remind_add(
    text: list[str] = typer.Argument(..., help="What to remind you about"),
    at: str = typer.Option(..., "--at", help='When, e.g. "in 20 minutes" or "tomorrow 9am"'),
    repeat: Optional[str] = typer.Option(None, "--repeat", help='e.g. "every day at 9am", "every 2 hours"'),
):
    """Add a reminder."""
    do_remind(" ".join(text), _when(at), repeat=repeat)


@remind_app.command("list")
def remind_list():
    """List pending reminders."""
    from friday.assistant.recurrence import describe

    reminders = _assistant().store.list_reminders()
    if not reminders:
        show_info("No pending reminders.")
        return
    table = Table(box=box.ROUNDED, border_style="bright_blue", title="[bold bright_cyan]Reminders[/]")
    table.add_column("#", justify="right", style="dim")
    table.add_column("When", style="cyan")
    table.add_column("Reminder")
    table.add_column("Repeats", style="dim")
    for r in reminders:
        table.add_row(str(r.id), escape(format_when(r.due)), Text(r.text), escape(describe(r.rule)) if r.rule else "")
    console.print(table)


@remind_app.command("rm")
def remind_rm(reminder_id: int = typer.Argument(..., help="Reminder number")):
    """Delete a reminder."""
    if not _assistant().store.delete_reminder(reminder_id):
        _fail(f"No reminder #{reminder_id}.")
    _ok(f"Deleted reminder #{reminder_id}")


# ── automate ───────────────────────────────────────────────────────────


@automate_app.command("add")
def automate_add(
    schedule_: str = typer.Argument(..., metavar="SCHEDULE", help='e.g. "every weekday at 8am"'),
    brief: bool = typer.Option(False, "--brief", help="Send the daily briefing"),
    notify_: Optional[str] = typer.Option(None, "--notify", help="Show this message"),
    task: Optional[str] = typer.Option(None, "--task", help="Run this task through an AI agent"),
    name: Optional[str] = typer.Option(None, "--name", help="Label for the automation"),
):
    """Create a recurring automation."""
    chosen = [a for a, v in (("briefing", brief), ("notify", notify_), ("agent_task", task)) if v]
    if len(chosen) != 1:
        _fail("Choose exactly one of --brief, --notify \"message\" or --task \"prompt\".", code=2)
    action = chosen[0]
    do_automate(schedule_, action, {"notify": notify_, "agent_task": task}.get(action), name=name)


@automate_app.command("list")
def automate_list():
    """List automations and their last result."""
    autos = _assistant().store.list_automations()
    if not autos:
        show_info('No automations. Try: friday automate add "every weekday at 8am" --brief')
        return
    table = Table(box=box.ROUNDED, border_style="bright_blue", title="[bold bright_cyan]Automations[/]")
    for col in ("#", "Name", "Schedule", "Action", "Next run", "Last"):
        table.add_column(col)
    for a in autos:
        state = "" if a.enabled else " [dim](paused)[/]"
        last = f"{a.last_status} {format_when(a.last_run)}" if a.last_run else "-"
        table.add_row(str(a.id), escape(a.name) + state, escape(a.schedule), a.action,
                      escape(format_when(a.next_run)) if a.enabled else "-", escape(last))
    console.print(table)


@automate_app.command("pause")
def automate_pause(automation_id: int):
    """Pause an automation."""
    if not _assistant().store.set_automation_enabled(automation_id, False):
        _fail(f"No automation #{automation_id}.")
    _ok(f"Paused automation #{automation_id}")


@automate_app.command("resume")
def automate_resume(automation_id: int):
    """Resume a paused automation (next run is recalculated from now)."""
    from friday.assistant.recurrence import next_run

    assistant = _assistant()
    auto = assistant.store.get_automation(automation_id)
    if not auto:
        _fail(f"No automation #{automation_id}.")
    assistant.store.set_automation_enabled(automation_id, True, next_run=next_run(auto.rule, datetime.now()))
    _ok(f"Resumed automation #{automation_id}")


@automate_app.command("rm")
def automate_rm(automation_id: int):
    """Delete an automation."""
    if not _assistant().store.delete_automation(automation_id):
        _fail(f"No automation #{automation_id}.")
    _ok(f"Deleted automation #{automation_id}")


@automate_app.command("run")
def automate_run(automation_id: int):
    """Run an automation now (doesn't change its schedule)."""
    from friday.assistant.daemon import run_automation

    assistant = _assistant()
    auto = assistant.store.get_automation(automation_id)
    if not auto:
        _fail(f"No automation #{automation_id}.")
    status, output = asyncio.run(run_automation(assistant, auto))
    assistant.store.finish_automation(automation_id, status, output)
    console.print(Text(output))
    if status != "ok":
        raise typer.Exit(1)


# ── daemon ─────────────────────────────────────────────────────────────


@daemon_app.command("run")
def daemon_run(
    tick: int = typer.Option(30, "--tick", min=5, help="Seconds between checks"),
    no_popups: bool = typer.Option(False, "--no-popups", help="Only log notifications, no desktop popups"),
):
    """Run in the foreground: deliver reminders and run automations."""
    import logging

    from friday.assistant.daemon import run_forever

    logging.getLogger().setLevel(logging.INFO)
    show_info("Friday daemon running. Press Ctrl+C to stop.")
    try:
        asyncio.run(run_forever(tick_seconds=tick, desktop=not no_popups))
    except KeyboardInterrupt:
        show_info("Stopped.")


@daemon_app.command("install")
def daemon_install():
    """Start the daemon automatically when you log in."""
    from friday.assistant.daemon import autostart_instructions, windows_install_command

    if sys.platform != "win32":
        console.print(Text(autostart_instructions()))
        return
    cmd = windows_install_command()
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        _fail(f"Task Scheduler refused: {(result.stderr or result.stdout).strip()}")
    _ok("Friday will start in the background each time you log in. Start it now with: friday daemon run")


@daemon_app.command("uninstall")
def daemon_uninstall():
    """Stop starting the daemon at log-in (Windows)."""
    from friday.assistant.daemon import TASK_NAME

    if sys.platform != "win32":
        show_info("Remove the systemd/launchd entry you created for Friday.")
        return
    result = subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], capture_output=True, text=True)
    if result.returncode != 0:
        _fail((result.stderr or result.stdout).strip())
    _ok("Removed the log-on task.")


# ── calendar ───────────────────────────────────────────────────────────


@calendar_app.command("export")
def calendar_export(
    path: Path = typer.Argument(Path("friday.ics"), help="Where to write the .ics file"),
):
    """Export appointments as an .ics file (import into Google, Outlook or Apple Calendar)."""
    count = _assistant().export_ics(path)
    _ok(f"Wrote {count} appointment(s) to {path.resolve()}")


def register(app: typer.Typer) -> None:
    """Attach assistant commands to the main Friday CLI."""
    app.command()(ask)
    app.command()(agenda)
    app.command()(schedule)
    app.command()(cancel)
    app.command()(free)
    app.add_typer(todo_app, name="todo")
    app.add_typer(remind_app, name="remind")
    app.add_typer(automate_app, name="automate")
    app.add_typer(daemon_app, name="daemon")
    app.add_typer(calendar_app, name="calendar")
