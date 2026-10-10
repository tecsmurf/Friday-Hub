"""
Natural-language command understanding for ``friday ask``.

Turns "remind me to call mom tomorrow at 6pm" or "book dentist friday 3pm for
45 minutes" into a structured :class:`Intent`. Clear phrasings are handled by
deterministic rules (offline, predictable); anything else is classified by the
local LLM, and requests that are not assistant actions go to Hermes as agent
tasks. Times are always parsed by :mod:`friday.assistant.timeparse`, never
trusted from the LLM.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime

from friday.assistant.timeparse import (
    _MONTHS,
    _WEEKDAY_ALIASES,
    TimeParseError,
    parse_datetime,
    parse_duration,
)

log = logging.getLogger(__name__)

KINDS = ("todo", "remind", "schedule", "agenda", "automate", "list_todos", "agent_task")


@dataclass
class Intent:
    kind: str
    title: str = ""
    when: datetime | None = None
    duration: int | None = None
    location: str | None = None
    repeat: str | None = None
    action: str | None = None
    source: str = "rules"
    extra: dict = field(default_factory=dict)


_WD = "|".join(sorted(_WEEKDAY_ALIASES, key=len, reverse=True))
_MO = "|".join(sorted(_MONTHS, key=len, reverse=True))
_CLOCK = r"\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)|\d{1,2}:\d{2}|noon|midnight|midday"
_TIME_PHRASES = [
    r"\bin\s+(?:\d+\s*(?:minutes?|mins?|hours?|hrs?|h|days?|weeks?)\s*(?:and\s+)?)+",
    r"\bin\s+(?:an?|one)\s+hour\b",
    r"\bin\s+half\s+an?\s+hour\b",
    r"\b\d{4}-\d{2}-\d{2}(?:[ t]\d{1,2}:\d{2})?\b",
    r"\b(?:day after tomorrow|tomorrow|tmrw|tmr|today|tonight|this (?:morning|afternoon|evening))\b",
    rf"\b(?:(?:next|this|on)\s+)?(?:{_WD})\b",
    rf"\b(?:on\s+)?\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{_MO})\b",
    rf"\b(?:on\s+)?(?:{_MO})\s+\d{{1,2}}(?:st|nd|rd|th)?\b",
    rf"\b(?:at|@)\s*(?:{_CLOCK}|\d{{1,2}})\b",
    rf"\b(?:{_CLOCK})",
    r"\b(?:morning|afternoon|evening)\b",
]
_TIME_RE = re.compile("|".join(f"(?:{p})" for p in _TIME_PHRASES), re.I)
_DURATION_RE = re.compile(r"\bfor\s+(\d+\s*(?:minutes?|mins?|m|hours?|hrs?|h)(?:\s*(?:and\s+)?\d+\s*(?:minutes?|mins?|m))?)\b", re.I)
_LOCATION_RE = re.compile(r"\b(?:at|@)\s+(?!\d)([A-Z][\w'&.-]*(?:\s+[A-Z][\w'&.-]*)*)")


def _tidy(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip(" ,.;:-")
    return re.sub(r"^(?:to|that|about|for|a|an)\s+", "", text, flags=re.I).strip()


def extract_datetime(text: str, now: datetime | None = None) -> tuple[datetime | None, str]:
    """Find a date/time phrase inside free text. Returns (datetime, remaining text)."""
    spans = [m for m in _TIME_RE.finditer(text) if m.group(0).strip()]
    if not spans:
        return None, text
    phrase = " ".join(m.group(0) for m in spans)
    try:
        when = parse_datetime(phrase, now=now)
    except TimeParseError:
        return None, text
    remainder = text
    for m in reversed(spans):
        remainder = remainder[: m.start()] + " " + remainder[m.end():]
    return when, _tidy(remainder)


def _extract_duration(text: str) -> tuple[int | None, str]:
    match = _DURATION_RE.search(text)
    if not match:
        return None, text
    try:
        minutes = parse_duration(match.group(1))
    except TimeParseError:
        return None, text
    return minutes, text[: match.start()] + text[match.end():]


def _extract_location(text: str) -> tuple[str | None, str]:
    match = _LOCATION_RE.search(text)
    if not match:
        return None, text
    return match.group(1).strip(), text[: match.start()] + text[match.end():]


def parse_rules(text: str, now: datetime | None = None) -> Intent | None:
    """Deterministic parsing for clear phrasings. None when unsure."""
    raw = " ".join(text.split())
    lowered = raw.lower()

    if re.match(r"^(what'?s|what is|show( me)?|list)\b.*\b(agenda|schedule|calendar|day|plan)\b", lowered) or \
            re.match(r"^(agenda|my day|brief(ing)?|daily brief(ing)?)\b", lowered):
        day, _ = extract_datetime(raw, now=now)
        return Intent("agenda", when=day)

    if re.match(r"^(show|list|what are)\b.*\b(todos?|to-dos?|tasks|to do list|todo list)\b", lowered):
        return Intent("list_todos")

    m = re.match(r"^(?:every|each|daily|hourly|weekdays|weekends)\b", lowered)
    if m:
        # "every weekday at 8am brief me" / "every day at 9 remind me to stretch"
        sched = re.match(
            r"^((?:every|each)\s+[\w\s,:-]*?(?:\d{1,2}(?::\d{2})?\s*(?:am|pm)?|day|weekday|weekend|hour|minutes?|hours?|"
            rf"{_WD})s?|daily|hourly|weekdays|weekends)\b(.*)$",
            raw, re.I,
        )
        if sched:
            schedule, rest = sched.group(1), _tidy(sched.group(2))
            at = re.match(r"^(at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b(.*)$", rest, re.I)
            if at:
                schedule, rest = f"{schedule} {at.group(1)}", _tidy(at.group(2))
            rest_l = rest.lower()
            if re.search(r"\b(brief|briefing|agenda|summary of my day)\b", rest_l):
                return Intent("automate", repeat=schedule, action="briefing", title="Daily briefing")
            remind = re.match(r"^remind me (?:to |about )?(.+)$", rest, re.I)
            if remind:
                return Intent("automate", repeat=schedule, action="notify", title=_tidy(remind.group(1)))
            if rest:
                return Intent("automate", repeat=schedule, action="agent_task", title=rest)

    m = re.match(r"^(?:remind me|set (?:a )?reminder|reminder)\b(.*)$", raw, re.I)
    if m:
        body = m.group(1)
        repeat = None
        rep = re.search(r"\b(every\s+[\w\s:]+?|daily|hourly|weekdays|weekends)\s*$", body, re.I)
        if rep:
            repeat, body = rep.group(1), body[: rep.start()]
        when, rest = extract_datetime(body, now=now)
        if when is None and repeat is None:
            return None
        return Intent("remind", title=_tidy(rest) or "Reminder", when=when, repeat=repeat)

    m = re.match(
        r"^(?:schedule|book|set up|setup|arrange|add|create|make)\s+(?:an?\s+|my\s+)?"
        r"(?:(?:appointment|meeting|call|event)\s+(?:with\s+|for\s+)?)?(.*)$",
        raw, re.I,
    )
    is_booking = bool(m) and (
        re.match(r"^(schedule|book|set up|setup|arrange)\b", lowered)
        or re.search(r"\b(appointment|meeting|event|call)\b", lowered)
    )
    if not is_booking and re.search(r"^(?:an?\s+)?(appointment|meeting|call)\s+with\b", lowered):
        m, is_booking = re.match(r"^(.*)$", raw), True
    if m and is_booking:
        body = m.group(1)
        duration, body = _extract_duration(body)
        location, body = _extract_location(body)
        when, rest = extract_datetime(body, now=now)
        if when is None:
            return None
        title = _tidy(re.sub(r"\b(appointment|meeting)\b", "", rest, flags=re.I)) or "Appointment"
        if re.search(r"\bwith\b", lowered) and not title.lower().startswith(("with", "call", "meeting")):
            if re.search(r"\b(?:appointment|meeting|call)\s+with\b", lowered):
                title = f"Meeting with {title}" if "meeting" in lowered else (
                    f"Call with {title}" if "call" in lowered else f"Appointment with {title}")
        return Intent("schedule", title=title[0].upper() + title[1:], when=when, duration=duration, location=location)

    m = re.match(r"^(?:add\s+)?(?:a\s+)?(?:todo|to-do|task)\s*:?\s+(.+)$", raw, re.I) or \
        re.match(r"^add\s+(.+?)\s+to\s+(?:my\s+)?(?:todo|to-do|task)?\s*list$", raw, re.I)
    if m:
        body = m.group(1)
        when, rest = extract_datetime(re.sub(r"\b(?:by|due|before)\b", " ", body, flags=re.I), now=now)
        return Intent("todo", title=_tidy(rest) or _tidy(body), when=when)

    return None


_LLM_PROMPT = """You classify a user's request to a personal assistant called Friday.
Respond with ONLY a JSON object:
{{"kind": "<todo|remind|schedule|agenda|automate|list_todos|agent_task>",
  "title": "<short title/what, no date words>",
  "when": "<the date/time words exactly as the user wrote them, or empty>",
  "duration": "<duration words like '45 minutes', or empty>",
  "location": "<place, or empty>",
  "repeat": "<recurrence words like 'every weekday at 8am', or empty>",
  "action": "<for automate: briefing|notify|agent_task>"}}

Kinds: todo = add to-do item; remind = one-off or repeating reminder; schedule = book an
appointment/meeting/event; agenda = show plans for a day; automate = do something on a
recurring schedule; list_todos = show to-dos; agent_task = anything else, e.g. coding,
research or computer work for an AI agent.

Request: "{text}"
"""


async def parse_with_llm(text: str, now: datetime | None = None) -> Intent:
    """Classify with the local LLM. Raises on any failure (caller falls back)."""
    from friday.models import router as model_router
    from friday.models.parsing import parse_json_object

    response = await model_router.chat(
        messages=[{"role": "user", "content": _LLM_PROMPT.format(text=text.replace('"', "'"))}],
        role="fast",
        json_mode=True,
    )
    data = parse_json_object(response["content"])
    kind = data.get("kind")
    if kind not in KINDS:
        raise ValueError(f"unknown intent {kind!r}")

    def opt(key):
        value = data.get(key)
        return str(value).strip() if value not in (None, "") else None

    when = None
    if opt("when"):
        try:
            when = parse_datetime(opt("when"), now=now)
        except TimeParseError:
            when = None
    duration = None
    if opt("duration"):
        try:
            duration = parse_duration(opt("duration"))
        except TimeParseError:
            duration = None
    action = opt("action") if opt("action") in ("briefing", "notify", "agent_task") else None
    return Intent(kind, title=opt("title") or text, when=when, duration=duration, location=opt("location"),
                  repeat=opt("repeat"), action=action, source="llm")


async def understand(text: str, now: datetime | None = None, use_llm: bool = True) -> Intent:
    """Rules first, then the LLM, then treat it as an agent task."""
    intent = parse_rules(text, now=now)
    if intent:
        return intent
    if use_llm:
        try:
            return await parse_with_llm(text, now=now)
        except Exception as exc:
            log.info("LLM intent parsing unavailable: %s", exc)
    return Intent("agent_task", title=text, source="fallback")
