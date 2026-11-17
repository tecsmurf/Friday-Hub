"""
Voice conversations: hear a request, carry it out through ``friday ask``,
say the result.

Two ways to talk:

* push-to-talk (default): press Enter, speak, and Friday stops listening when
  you pause. You can also just type a request at the prompt.
* hands-free (``--wake``): Friday listens in the background and responds when
  a sentence starts with the wake word, e.g. "Friday, what's on today?". Say
  only "Friday" and it answers "Yes?" and listens for the request.
"""

import asyncio
import re
from dataclasses import dataclass
from typing import Callable, Iterator

import click
import typer
from rich.text import Text

from friday.display import console

_FILLERS = r"(?:hey|hi|hello|ok|okay|yo|oh|um|uh|so|and)"
# Ways Whisper may write "Friday" when it is said as a name.
_WAKE_VARIANTS = ("friday", "fri day", "fry day", "fryday", "frieday", "fridays", "friday's")
_EXIT_RE = re.compile(r"^(?:(?:ok(?:ay)?|thanks?|thank you)[, ]+)?(?:goodbye|good bye|bye(?: bye)?|exit|quit|"
                      r"stop listening|that'?s all|that is all|go to sleep|we'?re done|see you(?: later)?)"
                      r"(?:[, ]+(?:friday|thanks?|thank you))?$", re.I)
_CANCEL_RE = re.compile(r"^(?:never ?mind|cancel|forget it|nothing)$", re.I)
# How a command that failed (and printed why) ends.
_STOPS = tuple({SystemExit, click.exceptions.Exit, click.exceptions.Abort, typer.Exit, typer.Abort})
_SLOW_KINDS = ("research", "web_task", "routine", "agent_task")


def wake_pattern(wake_word: str = "friday") -> re.Pattern:
    words = {wake_word.lower().strip()}
    if wake_word.lower().strip() == "friday":
        words.update(_WAKE_VARIANTS)
    alts = "|".join(re.escape(w).replace(r"\ ", r"\s*") for w in sorted(words, key=len, reverse=True))
    return re.compile(rf"^\W*(?:{_FILLERS}\W+){{0,2}}(?:{alts})\b\W*(.*)$", re.I | re.S)


def split_wake(text: str, wake_word: str = "friday") -> tuple[bool, str]:
    """Whether ``text`` starts with the wake word, and the request after it.

    Only the start counts, so "move my meeting to Friday" doesn't wake it.
    """
    match = wake_pattern(wake_word).match(text.strip())
    if not match:
        return False, ""
    return True, clean_command(match.group(1))


def clean_command(text: str) -> str:
    """Tidy a transcript for ``ask``: trim, drop end punctuation, "please"."""
    text = re.sub(r"\s+", " ", text).strip()
    # Whisper writes "6 p.m." / "6 P.M."; the time parser wants "6pm".
    text = re.sub(r"(\d)\s*([ap])\.?\s?m\b\.?", lambda m: f"{m.group(1)}{m.group(2).lower()}m", text, flags=re.I)
    text = text.strip(" ,;:-")
    text = re.sub(r"[.!?]+$", "", text).strip()
    text = re.sub(r"^(?:please|can you|could you|would you)\s+", "", text, flags=re.I)
    text = re.sub(r"[,\s]+please$", "", text, flags=re.I)
    return text.strip(" ,")


def is_exit(text: str) -> bool:
    return bool(_EXIT_RE.match(clean_command(text)))


# ── Running a request and building the spoken answer ───────────────────


def _spoken_todos() -> str:
    from friday.assistant import cli as assistant_cli
    from friday.assistant.timeparse import format_when

    todos = assistant_cli._assistant().store.list_todos()
    if not todos:
        return "Your todo list is empty."
    items = [t.title + (f", due {format_when(t.due)}" if t.due else "") for t in todos[:5]]
    more = f" and {len(todos) - 5} more" if len(todos) > 5 else ""
    noun = "todo" if len(todos) == 1 else "todos"
    return f"You have {len(todos)} {noun}: " + "; ".join(items) + more + "."


def handle(text: str, understand=None, run_intent=None, say: Callable[[str], None] | None = None) -> str:
    """Carry out one request; return what Friday should say about it.

    Output is still printed as usual; it's also recorded so the spoken reply
    can be built from it.
    """
    from friday.assistant import cli as assistant_cli
    from friday.voice.speech import to_speech

    if understand is None:
        from friday.assistant.intent import understand
    run_intent = run_intent or assistant_cli.run_intent

    intent = asyncio.run(understand(text, use_llm=True))
    if say and intent.kind in _SLOW_KINDS:
        say("On it. This may take a minute.")

    console.record = True
    console.export_text(clear=True)  # drop anything recorded earlier
    try:
        run_intent(intent, text)
    except _STOPS:
        pass  # a failed action already printed why, and that becomes the reply
    except KeyboardInterrupt:
        return "Stopped."
    finally:
        printed = console.export_text(clear=True)
        console.record = False

    if intent.kind == "list_todos":
        return _spoken_todos()
    if intent.kind == "agenda":
        return to_speech(printed, limit=600)
    return to_speech(printed) or "Done."


@dataclass
class Conversation:
    """One voice session. ``listen`` returns the next request (or None)."""

    speaker: object
    say_out_loud: bool = True
    handler: Callable[..., str] | None = None  # default: handle()

    def reply(self, message: str) -> None:
        console.print(Text(f"  Friday: {message}", style="bright_cyan"))
        if self.say_out_loud and message:
            try:
                self.speaker.say(message)
            except Exception as exc:  # speech is a nicety; never lose the session over it
                console.print(Text(f"  (couldn't speak: {exc})", style="dim"))

    def take(self, text: str) -> bool:
        """Handle one heard/typed request. Returns False to end the session."""
        command = clean_command(text)
        if not command:
            return True
        if is_exit(command):
            self.reply("Goodbye.")
            return False
        if _CANCEL_RE.match(command):
            self.reply("Okay.")
            return True
        console.print(Text(f"  You: {command}", style="bold"))
        try:
            answer = (self.handler or handle)(command, say=self.reply)
        except Exception as exc:
            answer = f"Sorry, something went wrong: {exc}"
        self.reply(answer)
        return True

    def run(self, requests: Iterator[str]) -> None:
        for text in requests:
            if not self.take(text):
                return
