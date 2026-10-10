"""
Deterministic natural-language date/time parsing.

Covers the phrasings people use for reminders and appointments without an LLM
or extra dependency, so scheduling works offline and predictably:

    "in 20 minutes", "in 2 hours", "tomorrow at 3pm", "today 18:30",
    "friday 10am", "next monday at 9", "on 2026-10-14 at 14:00",
    "14 oct 3pm", "oct 14", "tonight", "noon", "at 7:15pm"

All results are naive local datetimes (the machine's local time). A bare
"at 1" … "at 7" without am/pm is read as afternoon/evening.
"""

import re
from datetime import datetime, timedelta

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_WEEKDAY_ALIASES = {d[:3]: i for i, d in enumerate(WEEKDAYS)} | {d: i for i, d in enumerate(WEEKDAYS)}
_WEEKDAY_ALIASES |= {"tues": 1, "wed": 2, "thur": 3, "thurs": 3}
_MONTHS = {
    m: i + 1
    for i, names in enumerate([
        ("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"),
        ("may",), ("jun", "june"), ("jul", "july"), ("aug", "august"),
        ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"), ("dec", "december"),
    ])
    for m in names
}
_UNITS = {
    "min": 1, "mins": 1, "minute": 1, "minutes": 1, "m": 1,
    "h": 60, "hr": 60, "hrs": 60, "hour": 60, "hours": 60,
    "day": 1440, "days": 1440, "week": 10080, "weeks": 10080,
}

_TIME_RE = re.compile(
    r"\b(?:at\s+)?(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>am|pm|a\.m\.|p\.m\.)?(?![\d/-])",
    re.I,
)

DEFAULT_HOUR = 9  # when a date is given without a time


class TimeParseError(ValueError):
    """The text does not contain a recognisable date/time."""


def _apply_time(base: datetime, hour: int, minute: int) -> datetime:
    return base.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _find_time(text: str) -> tuple[int, int] | None:
    """Find an explicit clock time. Bare numbers only count with at/am/pm/colon."""
    lowered = text.lower()
    named = {"noon": (12, 0), "midday": (12, 0), "midnight": (0, 0)}
    for word, value in named.items():
        if re.search(rf"\b{word}\b", lowered):
            return value

    for match in _TIME_RE.finditer(lowered):
        has_marker = match.group("m") or match.group("ap") or match.group(0).strip().startswith("at")
        if not has_marker:
            continue
        hour = int(match.group("h"))
        minute = int(match.group("m") or 0)
        ap = (match.group("ap") or "").replace(".", "")
        if not ap and not match.group("m") and 1 <= hour <= 7:
            # "at 2" almost always means 2 pm for appointments and reminders.
            hour += 12
        if ap == "pm" and hour < 12:
            hour += 12
        elif ap == "am" and hour == 12:
            hour = 0
        if hour > 23 or minute > 59:
            continue
        return hour, minute
    return None


def parse_datetime(text: str, now: datetime | None = None) -> datetime:
    """Parse a date/time expression relative to ``now``.

    Raises:
        TimeParseError: if nothing recognisable is found.
    """
    now = (now or datetime.now()).replace(second=0, microsecond=0)
    lowered = " ".join(text.lower().split())
    if not lowered:
        raise TimeParseError("empty time expression")

    # ISO forms: 2026-10-14, 2026-10-14 14:00, 2026-10-14T14:00
    iso = re.search(r"\b(\d{4})-(\d{2})-(\d{2})(?:[ t](\d{1,2}):(\d{2}))?\b", lowered)
    if iso:
        y, mo, d = (int(iso.group(i)) for i in (1, 2, 3))
        base = datetime(y, mo, d)
        if iso.group(4):
            return _apply_time(base, int(iso.group(4)), int(iso.group(5)))
        clock = _find_time(lowered[: iso.start()] + lowered[iso.end():])
        return _apply_time(base, *(clock or (DEFAULT_HOUR, 0)))

    # Relative offsets: "in 20 minutes", "in 1 hour 30 min", "in 2 days"
    rel = re.match(r"^(?:in\s+)((?:\d+\s*[a-z]+\s*(?:and\s+)?)+)$", lowered) or re.search(
        r"\bin\s+((?:\d+\s*[a-z]+\s*(?:and\s+)?)+)", lowered
    )
    if rel:
        total = 0
        for amount, unit in re.findall(r"(\d+)\s*([a-z]+)", rel.group(1)):
            if unit in _UNITS:
                total += int(amount) * _UNITS[unit]
        if total:
            result = now + timedelta(minutes=total)
            clock = _find_time(lowered.replace(rel.group(0), " "))
            if clock and total % 1440 == 0:  # "in 2 days at 5pm"
                result = _apply_time(result, *clock)
            return result
    if re.search(r"\bin\s+(?:an?|one)\s+hour\b", lowered):
        return now + timedelta(hours=1)
    if re.search(r"\bin\s+half\s+an?\s+hour\b", lowered):
        return now + timedelta(minutes=30)

    clock = _find_time(lowered)
    day: datetime | None = None

    if re.search(r"\b(today|tonight|this evening|this afternoon|this morning)\b", lowered):
        day = now
        if clock is None:
            if "tonight" in lowered or "evening" in lowered:
                clock = (20, 0)
            elif "afternoon" in lowered:
                clock = (15, 0)
            elif "morning" in lowered:
                clock = (DEFAULT_HOUR, 0)
    elif re.search(r"\bday after tomorrow\b", lowered):
        day = now + timedelta(days=2)
    elif re.search(r"\b(tomorrow|tmrw|tmr)\b", lowered):
        day = now + timedelta(days=1)
        if clock is None:
            if "evening" in lowered or "night" in lowered:
                clock = (20, 0)
            elif "afternoon" in lowered:
                clock = (15, 0)
    else:
        wd = re.search(r"\b(next\s+|this\s+|on\s+)?(" + "|".join(sorted(_WEEKDAY_ALIASES, key=len, reverse=True)) + r")\b", lowered)
        if wd:
            target = _WEEKDAY_ALIASES[wd.group(2)]
            delta = (target - now.weekday()) % 7
            if delta == 0:
                # "monday" said on a Monday: today if the time is still ahead, else next week
                hm = clock or (DEFAULT_HOUR, 0)
                if (wd.group(1) or "").strip() == "next" or _apply_time(now, *hm) <= now:
                    delta = 7
            elif (wd.group(1) or "").strip() == "next" and delta < 7:
                delta = delta if delta > 0 else 7
            day = now + timedelta(days=delta)
        else:
            md = re.search(
                r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(" + "|".join(_MONTHS) + r")\b|\b("
                + "|".join(_MONTHS) + r")\s+(\d{1,2})(?:st|nd|rd|th)?\b",
                lowered,
            )
            if md:
                if md.group(1):
                    d, mo = int(md.group(1)), _MONTHS[md.group(2)]
                else:
                    d, mo = int(md.group(4)), _MONTHS[md.group(3)]
                candidate = datetime(now.year, mo, d)
                hm = clock or (DEFAULT_HOUR, 0)
                if _apply_time(candidate, *hm) < now:
                    candidate = candidate.replace(year=now.year + 1)
                day = candidate
                # Strip the day number so it is not mistaken for a clock time.
                clock = _find_time(lowered[: md.start()] + " " + lowered[md.end():])

    if day is None:
        if clock is None:
            raise TimeParseError(f"could not find a date or time in {text!r}")
        # Time only: today if still ahead, otherwise tomorrow.
        candidate = _apply_time(now, *clock)
        return candidate if candidate > now else candidate + timedelta(days=1)

    return _apply_time(day, *(clock or (DEFAULT_HOUR, 0)))


def parse_duration(text: str) -> int:
    """Parse "45m", "1h", "1 hour 30 minutes", "90" (minutes) into minutes."""
    lowered = text.strip().lower()
    if lowered.isdigit():
        return int(lowered)
    total = 0
    for amount, unit in re.findall(r"(\d+)\s*([a-z]+)", lowered):
        if unit not in _UNITS:
            raise TimeParseError(f"unknown duration unit {unit!r}")
        total += int(amount) * _UNITS[unit]
    if not total:
        raise TimeParseError(f"could not parse duration {text!r}")
    return total


def format_when(when: datetime, now: datetime | None = None) -> str:
    """Human-friendly rendering: 'today 15:00', 'tomorrow 09:30', 'Fri 14 Oct 10:00'."""
    now = now or datetime.now()
    clock = when.strftime("%H:%M")
    days = (when.date() - now.date()).days
    if days == 0:
        return f"today {clock}"
    if days == 1:
        return f"tomorrow {clock}"
    if days == -1:
        return f"yesterday {clock}"
    if 1 < days < 7:
        return f"{when.strftime('%a')} {clock}"
    return when.strftime("%a %d %b %Y %H:%M")
