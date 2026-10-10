"""
Recurrence rules for automations and repeating reminders.

Supported phrasings (case-insensitive):

    "every day at 9am", "daily 07:30", "every weekday at 8:45",
    "weekends at 10", "every monday and thursday at 18:00",
    "every 2 hours", "every 30 minutes", "hourly"

A rule is a small JSON-serialisable dict:
    {"kind": "interval", "minutes": 120}
    {"kind": "weekly", "days": [0, 3], "time": "18:00"}   # 0 = Monday
"""

import re
from datetime import datetime, timedelta

from friday.assistant.timeparse import _UNITS, _WEEKDAY_ALIASES, TimeParseError, _find_time

MIN_INTERVAL_MINUTES = 5


def parse_recurrence(text: str) -> dict:
    """Parse a recurrence phrase into a rule dict. Raises TimeParseError."""
    lowered = " ".join(text.lower().split())

    if re.search(r"\bhourly\b", lowered):
        return {"kind": "interval", "minutes": 60}

    interval = re.search(r"\bevery\s+(\d+)\s*([a-z]+)", lowered)
    if interval and interval.group(2) in _UNITS and _UNITS[interval.group(2)] < 1440:
        minutes = int(interval.group(1)) * _UNITS[interval.group(2)]
        if minutes < MIN_INTERVAL_MINUTES:
            raise TimeParseError(f"interval must be at least {MIN_INTERVAL_MINUTES} minutes")
        return {"kind": "interval", "minutes": minutes}
    if re.search(r"\bevery\s+(an?\s+)?hour\b", lowered):
        return {"kind": "interval", "minutes": 60}

    clock = _find_time(lowered)
    if clock is None:
        bare = re.search(r"\b(?:at\s+)?(\d{1,2})\b(?!\s*(?:min|hour|day))", lowered)
        if bare and int(bare.group(1)) < 24:
            clock = (int(bare.group(1)), 0)
    time_str = f"{clock[0]:02d}:{clock[1]:02d}" if clock else "09:00"

    if re.search(r"\b(weekdays?|every\s+workday|workdays|mon(day)?\s*-\s*fri(day)?)\b", lowered):
        days = [0, 1, 2, 3, 4]
    elif re.search(r"\bweekends?\b", lowered):
        days = [5, 6]
    elif re.search(r"\b(daily|every\s+day|everyday|each\s+day|every\s+morning|every\s+evening|every\s+night)\b", lowered):
        days = list(range(7))
        if clock is None and "evening" in lowered:
            time_str = "18:00"
        elif clock is None and "night" in lowered:
            time_str = "21:00"
    else:
        names = sorted(_WEEKDAY_ALIASES, key=len, reverse=True)
        found = re.findall(r"\b(" + "|".join(names) + r")s?\b", lowered)
        days = sorted({_WEEKDAY_ALIASES[n] for n in found})
        if not days:
            raise TimeParseError(f"could not understand recurrence {text!r}")

    return {"kind": "weekly", "days": days, "time": time_str}


def next_run(rule: dict, after: datetime) -> datetime:
    """The first run strictly after ``after``."""
    after = after.replace(second=0, microsecond=0)
    if rule["kind"] == "interval":
        return after + timedelta(minutes=int(rule["minutes"]))
    if rule["kind"] == "weekly":
        hour, minute = (int(x) for x in rule["time"].split(":"))
        for offset in range(0, 8):
            day = after + timedelta(days=offset)
            candidate = day.replace(hour=hour, minute=minute)
            if candidate.weekday() in rule["days"] and candidate > after:
                return candidate
    raise ValueError(f"invalid recurrence rule: {rule!r}")


def describe(rule: dict) -> str:
    """Short human description of a rule."""
    if rule["kind"] == "interval":
        minutes = int(rule["minutes"])
        if minutes % 60 == 0:
            hours = minutes // 60
            return "every hour" if hours == 1 else f"every {hours} hours"
        return f"every {minutes} minutes"
    days = rule["days"]
    if days == list(range(7)):
        label = "every day"
    elif days == [0, 1, 2, 3, 4]:
        label = "weekdays"
    elif days == [5, 6]:
        label = "weekends"
    else:
        label = ", ".join(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][d] for d in days)
    return f"{label} at {rule['time']}"
