"""
Background daemon: delivers reminders and runs automations on schedule.

Run it with ``friday daemon run``; ``friday daemon install`` registers it to
start at log-on (Windows Task Scheduler). Each tick:

1. Every reminder whose time has come is delivered once (repeating reminders
   move to their next occurrence).
2. Every automation whose ``next_run`` has passed is *claimed* (its next run is
   advanced first, so a crash or a second daemon never runs it twice), then
   executed, and its status/output recorded.

Work missed while the computer was off runs once on the next start; it is not
replayed once per missed occurrence.
"""

import asyncio
import logging
import os
import shlex
import sys
from datetime import datetime, timedelta
from pathlib import Path

from friday.assistant.notify import notify
from friday.assistant.recurrence import next_run
from friday.assistant.service import Assistant
from friday.assistant.store import AssistantStore, Automation
from friday.assistant.timeparse import format_when
from friday.config import ROOT_DIR

log = logging.getLogger(__name__)

TICK_SECONDS = 30
BRIEFING_DIR = ROOT_DIR / "data" / "briefings"
TASK_NAME = "FridayHubDaemon"


async def _run_agent_task(prompt: str) -> tuple[bool, str]:
    from friday.brain.hermes import HermesBrain

    brain = HermesBrain()
    result = await brain.think(prompt)
    text = result.get("output") or result.get("error") or ""
    return bool(result.get("success")), text


async def run_automation(assistant: Assistant, automation: Automation, desktop: bool = True) -> tuple[str, str]:
    """Execute one automation's action. Returns (status, output)."""
    if automation.action == "briefing":
        text = assistant.briefing()
        BRIEFING_DIR.mkdir(parents=True, exist_ok=True)
        path = BRIEFING_DIR / f"{datetime.now():%Y-%m-%d_%H%M}.txt"
        path.write_text(text, encoding="utf-8")
        summary = text.split("\n", 2)
        notify("Friday briefing", "\n".join(summary[1:])[:250] or text[:250], desktop=desktop)
        return "ok", text
    if automation.action == "notify":
        notify(f"Friday: {automation.name}", automation.payload or "", desktop=desktop)
        return "ok", automation.payload or ""
    if automation.action == "agent_task":
        ok, output = await _run_agent_task(automation.payload or "")
        notify(
            f"Friday automation {'done' if ok else 'failed'}: {automation.name}",
            output.strip()[:250] or "(no output)",
            desktop=desktop,
        )
        return ("ok" if ok else "failed"), output
    return "failed", f"unknown action {automation.action}"


async def tick(assistant: Assistant, now: datetime | None = None, desktop: bool = True) -> dict:
    """Process everything due at ``now``. Returns counts for logging/tests."""
    now = now or datetime.now()
    store = assistant.store
    delivered = ran = 0

    for reminder in store.due_reminders(now):
        nxt = next_run(reminder.rule, now) if reminder.rule else None
        if not store.mark_reminder_delivered(reminder, next_due=nxt):
            continue  # another process delivered it
        late = now - reminder.due
        suffix = f" (was due {format_when(reminder.due, now)})" if late > timedelta(minutes=10) else ""
        notify("Friday reminder", reminder.text + suffix, desktop=desktop)
        delivered += 1

    for automation in store.due_automations(now):
        if not store.claim_automation(automation, next_run(automation.rule, now)):
            continue
        try:
            status, output = await run_automation(assistant, automation, desktop=desktop)
        except Exception as exc:  # an automation must never kill the daemon
            log.exception("Automation %s failed", automation.id)
            status, output = "failed", str(exc)
        store.finish_automation(automation.id, status, output)
        ran += 1

    synced = 0
    if assistant.calendar is not None:
        synced = assistant.sync_pending(now)
        for warning in assistant.warnings:
            log.warning(warning)
        assistant.warnings.clear()

    return {"reminders": delivered, "automations": ran, "synced": synced}


async def run_forever(tick_seconds: int = TICK_SECONDS, desktop: bool = True) -> None:
    log.info("Friday daemon started (tick %ss)", tick_seconds)
    from friday.assistant import google_calendar

    with AssistantStore() as store:
        assistant = Assistant(store)
        while True:
            if assistant.calendar is None:  # picks up `friday calendar connect` without a restart
                assistant.calendar = google_calendar.get_provider()
            try:
                counts = await tick(assistant, desktop=desktop)
                if any(counts.values()):
                    log.info("Delivered %(reminders)s reminder(s), ran %(automations)s automation(s), "
                             "synced %(synced)s appointment(s)", counts)
            except Exception:
                log.exception("Daemon tick failed")
            await asyncio.sleep(tick_seconds)


def daemon_launch_command() -> list[str]:
    """Command that starts the daemon with the current interpreter (windowless on Windows)."""
    python = Path(sys.executable)
    if sys.platform == "win32":
        pythonw = python.with_name("pythonw.exe")
        if pythonw.exists():
            python = pythonw
    return [str(python), "-m", "friday", "daemon", "run"]


def windows_install_command() -> list[str]:
    """schtasks command registering the daemon to start at log-on for this user."""
    launch = daemon_launch_command()
    task_run = " ".join(f'"{part}"' if " " in part else part for part in launch)
    return ["schtasks", "/Create", "/SC", "ONLOGON", "/TN", TASK_NAME, "/TR", task_run, "/RL", "LIMITED", "/F"]


def autostart_instructions() -> str:
    """Manual start-at-login instructions for platforms without automatic install."""
    cmd = " ".join(shlex.quote(p) for p in daemon_launch_command())
    home = os.environ.get("FRIDAY_HOME", "")
    env_line = f"Environment=FRIDAY_HOME={home}\n" if home else ""
    if sys.platform == "darwin":
        return (
            "Add a LaunchAgent (~/Library/LaunchAgents/com.friday.daemon.plist) whose\n"
            f"ProgramArguments are: {cmd}\nand load it with: launchctl load <plist>"
        )
    return (
        "Create ~/.config/systemd/user/friday.service:\n\n"
        "[Unit]\nDescription=Friday Hub daemon\n\n"
        f"[Service]\nExecStart={cmd}\n{env_line}Restart=on-failure\n\n"
        "[Install]\nWantedBy=default.target\n\n"
        "then run: systemctl --user enable --now friday.service"
    )
