"""
Assistant service — the personal-assistant operations Friday can perform.

Everything here is deterministic and local: scheduling appointments with
conflict checks and reminders, todos, reminders, recurring automations, the
daily briefing, and iCalendar (.ics) export so appointments can be imported
into Google Calendar, Outlook or Apple Calendar.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from friday.assistant.recurrence import describe, next_run, parse_recurrence
from friday.assistant.store import ACTIONS, AssistantStore, Automation, Event
from friday.assistant.timeparse import format_when
from friday.config import config

_cfg = config.get("assistant", {})
DEFAULT_DURATION = int(_cfg.get("default_duration_minutes", 30))
DEFAULT_REMIND_BEFORE = int(_cfg.get("remind_before_minutes", 15))
WORKDAY_START = str(_cfg.get("workday_start", "09:00"))
WORKDAY_END = str(_cfg.get("workday_end", "18:00"))


class SchedulingConflict(Exception):
    """The requested slot overlaps existing events."""

    def __init__(self, conflicts: list[Event], suggestion: datetime | None):
        self.conflicts = conflicts
        self.suggestion = suggestion
        names = ", ".join(f"{e.title} ({format_when(e.start)})" for e in conflicts)
        hint = f" Next free slot: {format_when(suggestion)}." if suggestion else ""
        super().__init__(f"That time overlaps {names}.{hint}")


@dataclass
class Scheduled:
    event: Event
    reminder_at: datetime | None


def _hm(value: str) -> tuple[int, int]:
    hour, minute = value.split(":")
    return int(hour), int(minute)


class Assistant:
    def __init__(self, store: AssistantStore | None = None, calendar="auto"):
        """
        Args:
            store: Local database (default: the runtime one).
            calendar: Remote calendar to sync with. ``"auto"`` uses Google
                Calendar when connected; ``None`` disables sync.
        """
        self.store = store or AssistantStore()
        if calendar == "auto":
            from friday.assistant import google_calendar

            calendar = google_calendar.get_provider()
        self.calendar = calendar
        self.warnings: list[str] = []

    # ── Remote calendar ─────────────────────────────────────────────

    def _remote_busy(self, start: datetime, end: datetime) -> list[Event]:
        """Remote events not created by Friday, as Event objects (id 0). Never raises."""
        if not self.calendar:
            return []
        try:
            remote = self.calendar.list_events(start, end)
        except Exception as exc:
            self._warn(f"Google Calendar unavailable, checked only Friday's calendar ({exc})")
            return []
        known = {e.external_id for e in self.store.events_between(start, end) if e.external_id}
        return [
            Event(0, r.title, r.start, r.end, r.location, None, "confirmed", "google", r.id, r.start)
            for r in remote
            if r.friday_id is None and r.id not in known and r.end > start and r.start < end
        ]

    def busy(self, start: datetime, end: datetime) -> list[Event]:
        """Local appointments plus remote busy events, ordered by start."""
        return sorted(self.store.events_between(start, end) + self._remote_busy(start, end), key=lambda e: e.start)

    def _warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    def _push(self, event: Event) -> bool:
        if not self.calendar or event.external_id:
            return False
        try:
            google_id = self.calendar.create_event(event)
        except Exception as exc:
            self._warn(f"Saved in Friday but not yet in Google Calendar ({exc}); it will sync automatically")
            return False
        self.store.set_event_external(event.id, "google", google_id)
        return True

    def sync_pending(self, now: datetime | None = None) -> int:
        """Push upcoming appointments that aren't in the remote calendar yet."""
        if not self.calendar:
            return 0
        now = now or datetime.now()
        pending = [e for e in self.store.events_between(now, now + timedelta(days=365)) if not e.external_id]
        return sum(1 for e in pending if self._push(e))

    # ── Appointments ────────────────────────────────────────────────

    def schedule(
        self,
        title: str,
        start: datetime,
        duration_minutes: int | None = None,
        location: str | None = None,
        notes: str | None = None,
        remind_before: int | None = None,
        allow_overlap: bool = False,
        now: datetime | None = None,
    ) -> Scheduled:
        """Book an appointment; raise SchedulingConflict instead of double-booking."""
        now = now or datetime.now()
        duration = duration_minutes or DEFAULT_DURATION
        if duration <= 0 or duration > 24 * 60:
            raise ValueError("duration must be between 1 minute and 24 hours")
        if start < now - timedelta(minutes=1):
            raise ValueError(f"{format_when(start, now)} is in the past")
        end = start + timedelta(minutes=duration)

        if not allow_overlap:
            clashes = self.busy(start, end)
            if clashes:
                raise SchedulingConflict(clashes, self.find_free_slot(duration, after=start, now=now))

        event = self.store.add_event(title, start, end, location=location, notes=notes)
        if self._push(event):
            event = self.store.get_event(event.id)
        lead = DEFAULT_REMIND_BEFORE if remind_before is None else remind_before
        reminder_at = None
        if lead >= 0:
            reminder_at = max(start - timedelta(minutes=lead), now)
            when = "now" if lead == 0 else f"in {lead} min"
            where = f" at {location}" if location else ""
            self.store.add_reminder(f"{title}{where} starts {when} ({start:%H:%M})", reminder_at, event_id=event.id)
        return Scheduled(event, reminder_at)

    def find_free_slot(
        self, duration_minutes: int, after: datetime | None = None, days: int = 14, now: datetime | None = None
    ) -> datetime | None:
        """First gap of the given length within working hours, on or after ``after``."""
        now = now or datetime.now()
        cursor = max(after or now, now).replace(second=0, microsecond=0)
        # Round up to the next quarter hour.
        cursor += timedelta(minutes=(-cursor.minute) % 15)
        need = timedelta(minutes=duration_minutes)
        ws, we = _hm(WORKDAY_START), _hm(WORKDAY_END)
        window_start = cursor.replace(hour=0, minute=0)
        busy = self.busy(window_start, window_start + timedelta(days=days + 1))

        for offset in range(days + 1):
            day = (cursor + timedelta(days=offset)).replace(hour=0, minute=0)
            day_start = day.replace(hour=ws[0], minute=ws[1])
            day_end = day.replace(hour=we[0], minute=we[1])
            slot = max(day_start, cursor) if offset == 0 else day_start
            for event in (e for e in busy if e.start < day_end and e.end > day_start):
                if event.start - slot >= need:
                    return slot
                slot = max(slot, event.end)
            if day_end - slot >= need:
                return slot
        return None

    def cancel(self, event_id: int) -> bool:
        event = self.store.get_event(event_id)
        if not self.store.cancel_event(event_id):
            return False
        if event and event.external_id and self.calendar:
            try:
                self.calendar.delete_event(event.external_id)
            except Exception as exc:
                self._warn(f"Cancelled in Friday, but could not remove it from Google Calendar ({exc})")
        return True

    # ── Todos & reminders ───────────────────────────────────────────

    def add_todo(self, title: str, due: datetime | None = None, priority: int = 2):
        return self.store.add_todo(title, due=due, priority=priority)

    def remind(self, text: str, when: datetime, repeat: str | None = None, now: datetime | None = None):
        now = now or datetime.now()
        rule = parse_recurrence(repeat) if repeat else None
        if rule and when <= now:
            when = next_run(rule, now)
        elif when < now - timedelta(minutes=1):
            raise ValueError(f"{format_when(when, now)} is in the past")
        return self.store.add_reminder(text, when, rule=rule)

    # ── Automations ─────────────────────────────────────────────────

    def automate(self, schedule: str, action: str, payload: str | None = None,
                 name: str | None = None, now: datetime | None = None) -> Automation:
        if action not in ACTIONS:
            raise ValueError(f"action must be one of: {', '.join(ACTIONS)}")
        rule = parse_recurrence(schedule)
        first = next_run(rule, now or datetime.now())
        label = name or {"briefing": "Daily briefing"}.get(action) or (payload or action)[:60]
        return self.store.add_automation(label, describe(rule), rule, action, payload, first)

    # ── Briefing ────────────────────────────────────────────────────

    def briefing(self, day: datetime | None = None, now: datetime | None = None) -> str:
        now = now or datetime.now()
        day = day or now
        agenda = self.store.agenda(day)
        offset = (day.date() - now.date()).days
        label = {0: "Today, ", 1: "Tomorrow, "}.get(offset, "")
        lines = [f"{label}{day:%A %d %B %Y}"]

        day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        events = sorted(agenda["events"] + self._remote_busy(day_start, day_start + timedelta(days=1)),
                        key=lambda e: e.start)
        if events:
            lines.append(f"\nAppointments ({len(events)}):")
            for e in events:
                where = f" @ {e.location}" if e.location else ""
                ref = f"#{e.id}" if e.id else "(Google)"
                lines.append(f"  {e.start:%H:%M}-{e.end:%H:%M}  {ref} {e.title}{where}")
        else:
            lines.append("\nNo appointments.")

        if agenda["reminders"]:
            lines.append("\nReminders:")
            for r in agenda["reminders"]:
                lines.append(f"  {r.due:%H:%M}  {r.text}")

        due = agenda["todos_due"]
        overdue = [t for t in due if t.due < day.replace(hour=0, minute=0, second=0, microsecond=0)]
        if due:
            lines.append(f"\nTodos due ({len(due)}{f', {len(overdue)} overdue' if overdue else ''}):")
            for t in due:
                flag = "!" if t.priority == 1 else " "
                late = " (overdue)" if t in overdue else ""
                lines.append(f" {flag}[{t.id}] {t.title}{late}")
        other = agenda["todos_other"]
        if other:
            lines.append(f"\nOther open todos: {len(other)}")
            for t in other[:5]:
                lines.append(f"  [{t.id}] {t.title}")

        free = self.find_free_slot(60, after=max(now, day.replace(hour=0, minute=0)), days=0, now=now)
        if free:
            lines.append(f"\nNext free hour: {free:%H:%M}")
        return "\n".join(lines)

    # ── iCalendar export ────────────────────────────────────────────

    def export_ics(self, path: Path, days_back: int = 30, days_ahead: int = 365) -> int:
        now = datetime.now()
        events = self.store.events_between(now - timedelta(days=days_back), now + timedelta(days=days_ahead))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(to_ics(events), encoding="utf-8", newline="")
        return len(events)


def _ics_escape(text: str) -> str:
    return (text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\n", "\\n"))


def _ics_fold(line: str) -> str:
    """Fold lines to 75 octets as RFC 5545 requires."""
    out, current = [], ""
    for ch in line:
        if len((current + ch).encode("utf-8")) > 75:
            out.append(current)
            current = " " + ch
        else:
            current += ch
    out.append(current)
    return "\r\n".join(out)


def _utc(dt: datetime) -> str:
    """Local naive datetime → UTC iCalendar timestamp."""
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def to_ics(events: list[Event]) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Friday Hub//Assistant//EN", "CALSCALE:GREGORIAN"]
    for e in events:
        lines += [
            "BEGIN:VEVENT",
            f"UID:friday-event-{e.id}@friday-hub",
            f"DTSTAMP:{stamp}",
            f"DTSTART:{_utc(e.start)}",
            f"DTEND:{_utc(e.end)}",
            f"SUMMARY:{_ics_escape(e.title)}",
        ]
        if e.location:
            lines.append(f"LOCATION:{_ics_escape(e.location)}")
        if e.notes:
            lines.append(f"DESCRIPTION:{_ics_escape(e.notes)}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(_ics_fold(line) for line in lines) + "\r\n"
