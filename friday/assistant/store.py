"""
Persistent assistant data: todos, calendar events, reminders and automations.

Stored in a local SQLite database (``<FRIDAY_HOME>/data/assistant.db`` by
default) so it survives restarts and can be shared safely between the CLI and
the background daemon (WAL mode + busy timeout). Times are naive local
datetimes stored as ISO-8601 strings.
"""

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from friday.config import ROOT_DIR, config

_assistant_cfg = config.get("assistant", {})
DEFAULT_DB_PATH = ROOT_DIR / _assistant_cfg.get("db_path", "data/assistant.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS todos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    notes TEXT,
    due TEXT,
    priority INTEGER NOT NULL DEFAULT 2,
    done_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    start TEXT NOT NULL,
    end TEXT NOT NULL,
    location TEXT,
    notes TEXT,
    status TEXT NOT NULL DEFAULT 'confirmed',
    provider TEXT NOT NULL DEFAULT 'local',
    external_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_start ON events(start);
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    due TEXT NOT NULL,
    rule TEXT,
    event_id INTEGER REFERENCES events(id) ON DELETE CASCADE,
    delivered_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(due);
CREATE TABLE IF NOT EXISTS automations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    schedule TEXT NOT NULL,
    rule TEXT NOT NULL,
    action TEXT NOT NULL,
    payload TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    next_run TEXT NOT NULL,
    last_run TEXT,
    last_status TEXT,
    last_output TEXT,
    created_at TEXT NOT NULL
);
"""

ACTIONS = ("briefing", "notify", "agent_task")


def _iso(value: datetime | None) -> str | None:
    return value.replace(microsecond=0).isoformat(sep=" ") if value else None


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


@dataclass
class Todo:
    id: int
    title: str
    notes: str | None
    due: datetime | None
    priority: int
    done_at: datetime | None
    created_at: datetime


@dataclass
class Event:
    id: int
    title: str
    start: datetime
    end: datetime
    location: str | None
    notes: str | None
    status: str
    provider: str
    external_id: str | None
    created_at: datetime


@dataclass
class Reminder:
    id: int
    text: str
    due: datetime
    rule: dict | None
    event_id: int | None
    delivered_at: datetime | None
    created_at: datetime


@dataclass
class Automation:
    id: int
    name: str
    schedule: str
    rule: dict
    action: str
    payload: str | None
    enabled: bool
    next_run: datetime
    last_run: datetime | None
    last_status: str | None
    last_output: str | None
    created_at: datetime = field(repr=False)


class AssistantStore:
    """Thin repository over the SQLite database."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else DEFAULT_DB_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(_SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @staticmethod
    def _now() -> str:
        return _iso(datetime.now())

    # ── Todos ───────────────────────────────────────────────────────

    def add_todo(self, title: str, due: datetime | None = None, priority: int = 2, notes: str | None = None) -> Todo:
        if not title.strip():
            raise ValueError("todo title is empty")
        if priority not in (1, 2, 3):
            raise ValueError("priority must be 1 (high), 2 (normal) or 3 (low)")
        cur = self.conn.execute(
            "INSERT INTO todos (title, notes, due, priority, created_at) VALUES (?, ?, ?, ?, ?)",
            (title.strip(), notes, _iso(due), priority, self._now()),
        )
        return self.get_todo(cur.lastrowid)

    def get_todo(self, todo_id: int) -> Todo | None:
        row = self.conn.execute("SELECT * FROM todos WHERE id = ?", (todo_id,)).fetchone()
        return self._todo(row) if row else None

    def list_todos(self, include_done: bool = False) -> list[Todo]:
        sql = "SELECT * FROM todos"
        if not include_done:
            sql += " WHERE done_at IS NULL"
        sql += " ORDER BY done_at IS NOT NULL, due IS NULL, due, priority, id"
        return [self._todo(r) for r in self.conn.execute(sql)]

    def complete_todo(self, todo_id: int) -> bool:
        cur = self.conn.execute(
            "UPDATE todos SET done_at = ? WHERE id = ? AND done_at IS NULL", (self._now(), todo_id)
        )
        return cur.rowcount == 1

    def delete_todo(self, todo_id: int) -> bool:
        return self.conn.execute("DELETE FROM todos WHERE id = ?", (todo_id,)).rowcount == 1

    @staticmethod
    def _todo(row) -> Todo:
        return Todo(row["id"], row["title"], row["notes"], _dt(row["due"]), row["priority"],
                    _dt(row["done_at"]), _dt(row["created_at"]))

    # ── Events ──────────────────────────────────────────────────────

    def add_event(
        self, title: str, start: datetime, end: datetime, location: str | None = None,
        notes: str | None = None, provider: str = "local", external_id: str | None = None,
    ) -> Event:
        if not title.strip():
            raise ValueError("event title is empty")
        if end <= start:
            raise ValueError("event must end after it starts")
        cur = self.conn.execute(
            "INSERT INTO events (title, start, end, location, notes, provider, external_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (title.strip(), _iso(start), _iso(end), location, notes, provider, external_id, self._now()),
        )
        return self.get_event(cur.lastrowid)

    def get_event(self, event_id: int) -> Event | None:
        row = self.conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return self._event(row) if row else None

    def events_between(self, start: datetime, end: datetime, include_cancelled: bool = False) -> list[Event]:
        sql = "SELECT * FROM events WHERE start < ? AND end > ?"
        if not include_cancelled:
            sql += " AND status != 'cancelled'"
        sql += " ORDER BY start"
        return [self._event(r) for r in self.conn.execute(sql, (_iso(end), _iso(start)))]

    def conflicts(self, start: datetime, end: datetime, ignore_id: int | None = None) -> list[Event]:
        return [e for e in self.events_between(start, end) if e.id != ignore_id]

    def cancel_event(self, event_id: int) -> bool:
        cur = self.conn.execute(
            "UPDATE events SET status = 'cancelled' WHERE id = ? AND status != 'cancelled'", (event_id,)
        )
        if cur.rowcount:
            self.conn.execute(
                "DELETE FROM reminders WHERE event_id = ? AND delivered_at IS NULL", (event_id,)
            )
        return cur.rowcount == 1

    def set_event_external(self, event_id: int, provider: str, external_id: str) -> None:
        self.conn.execute(
            "UPDATE events SET provider = ?, external_id = ? WHERE id = ?", (provider, external_id, event_id)
        )

    @staticmethod
    def _event(row) -> Event:
        return Event(row["id"], row["title"], _dt(row["start"]), _dt(row["end"]), row["location"],
                     row["notes"], row["status"], row["provider"], row["external_id"], _dt(row["created_at"]))

    # ── Reminders ───────────────────────────────────────────────────

    def add_reminder(self, text: str, due: datetime, rule: dict | None = None, event_id: int | None = None) -> Reminder:
        if not text.strip():
            raise ValueError("reminder text is empty")
        cur = self.conn.execute(
            "INSERT INTO reminders (text, due, rule, event_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (text.strip(), _iso(due), json.dumps(rule) if rule else None, event_id, self._now()),
        )
        return self.get_reminder(cur.lastrowid)

    def get_reminder(self, reminder_id: int) -> Reminder | None:
        row = self.conn.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
        return self._reminder(row) if row else None

    def list_reminders(self, include_delivered: bool = False) -> list[Reminder]:
        sql = "SELECT * FROM reminders"
        if not include_delivered:
            sql += " WHERE delivered_at IS NULL"
        sql += " ORDER BY due"
        return [self._reminder(r) for r in self.conn.execute(sql)]

    def due_reminders(self, now: datetime) -> list[Reminder]:
        rows = self.conn.execute(
            "SELECT * FROM reminders WHERE delivered_at IS NULL AND due <= ? ORDER BY due", (_iso(now),)
        )
        return [self._reminder(r) for r in rows]

    def mark_reminder_delivered(self, reminder: Reminder, next_due: datetime | None = None) -> bool:
        """Mark delivered; a repeating reminder is moved to its next occurrence instead.

        The update only applies if the reminder is unchanged since it was read,
        so two processes cannot both deliver it.
        """
        if next_due is not None:
            cur = self.conn.execute(
                "UPDATE reminders SET due = ? WHERE id = ? AND delivered_at IS NULL AND due = ?",
                (_iso(next_due), reminder.id, _iso(reminder.due)),
            )
        else:
            cur = self.conn.execute(
                "UPDATE reminders SET delivered_at = ? WHERE id = ? AND delivered_at IS NULL",
                (self._now(), reminder.id),
            )
        return cur.rowcount == 1

    def delete_reminder(self, reminder_id: int) -> bool:
        return self.conn.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,)).rowcount == 1

    @staticmethod
    def _reminder(row) -> Reminder:
        return Reminder(row["id"], row["text"], _dt(row["due"]), json.loads(row["rule"]) if row["rule"] else None,
                        row["event_id"], _dt(row["delivered_at"]), _dt(row["created_at"]))

    # ── Automations ─────────────────────────────────────────────────

    def add_automation(self, name: str, schedule: str, rule: dict, action: str,
                       payload: str | None, next_run: datetime) -> Automation:
        if action not in ACTIONS:
            raise ValueError(f"unknown action {action!r}; expected one of {', '.join(ACTIONS)}")
        if action in ("notify", "agent_task") and not (payload or "").strip():
            raise ValueError(f"action {action!r} needs a message or task")
        cur = self.conn.execute(
            "INSERT INTO automations (name, schedule, rule, action, payload, next_run, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name.strip() or action, schedule, json.dumps(rule), action, payload, _iso(next_run), self._now()),
        )
        return self.get_automation(cur.lastrowid)

    def get_automation(self, automation_id: int) -> Automation | None:
        row = self.conn.execute("SELECT * FROM automations WHERE id = ?", (automation_id,)).fetchone()
        return self._automation(row) if row else None

    def list_automations(self) -> list[Automation]:
        return [self._automation(r) for r in self.conn.execute("SELECT * FROM automations ORDER BY id")]

    def due_automations(self, now: datetime) -> list[Automation]:
        rows = self.conn.execute(
            "SELECT * FROM automations WHERE enabled = 1 AND next_run <= ? ORDER BY next_run", (_iso(now),)
        )
        return [self._automation(r) for r in rows]

    def claim_automation(self, automation: Automation, next_run: datetime) -> bool:
        """Advance next_run before running, so a crash or a second daemon can't double-run it."""
        cur = self.conn.execute(
            "UPDATE automations SET next_run = ?, last_run = ?, last_status = 'running' "
            "WHERE id = ? AND next_run = ?",
            (_iso(next_run), self._now(), automation.id, _iso(automation.next_run)),
        )
        return cur.rowcount == 1

    def finish_automation(self, automation_id: int, status: str, output: str) -> None:
        self.conn.execute(
            "UPDATE automations SET last_status = ?, last_output = ? WHERE id = ?",
            (status, output[:4000], automation_id),
        )

    def set_automation_enabled(self, automation_id: int, enabled: bool, next_run: datetime | None = None) -> bool:
        if next_run is not None:
            cur = self.conn.execute(
                "UPDATE automations SET enabled = ?, next_run = ? WHERE id = ?",
                (int(enabled), _iso(next_run), automation_id),
            )
        else:
            cur = self.conn.execute("UPDATE automations SET enabled = ? WHERE id = ?", (int(enabled), automation_id))
        return cur.rowcount == 1

    def delete_automation(self, automation_id: int) -> bool:
        return self.conn.execute("DELETE FROM automations WHERE id = ?", (automation_id,)).rowcount == 1

    @staticmethod
    def _automation(row) -> Automation:
        return Automation(row["id"], row["name"], row["schedule"], json.loads(row["rule"]), row["action"],
                          row["payload"], bool(row["enabled"]), _dt(row["next_run"]), _dt(row["last_run"]),
                          row["last_status"], row["last_output"], _dt(row["created_at"]))

    # ── Agenda ──────────────────────────────────────────────────────

    def agenda(self, day: datetime) -> dict:
        """Everything relevant to one calendar day."""
        start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        todos = self.list_todos()
        return {
            "date": start,
            "events": self.events_between(start, end),
            "reminders": [r for r in self.list_reminders() if start <= r.due < end],
            "todos_due": [t for t in todos if t.due and t.due < end],
            "todos_other": [t for t in todos if not t.due or t.due >= end],
        }
