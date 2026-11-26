"""
Text-to-speech: Friday talks back.

Engines, picked by ``voice.tts_engine`` (default ``auto``):

* ``pyttsx3`` - offline, in-process (SAPI5 on Windows, NSSpeech on macOS,
  espeak on Linux). Installed by ``pip install -e ".[voice]"``.
* ``system`` - the OS speech engine with no Python dependencies: Windows
  System.Speech through PowerShell, ``say`` on macOS, ``espeak-ng`` /
  ``espeak`` / ``spd-say`` on Linux. Text is passed in an environment variable
  or as a plain argv item, never interpolated into a script.
* ``none`` - stay silent (replies are still printed).

``auto`` tries pyttsx3, then the system engine, then silence.
"""

import logging
import os
import re
import shutil
import subprocess
import sys

log = logging.getLogger(__name__)

DEFAULT_RATE = 185  # words per minute

_PS_SPEAK = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
if ($env:FRIDAY_TTS_VOICE) {
  $match = $s.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Name -like "*$($env:FRIDAY_TTS_VOICE)*" } | Select-Object -First 1
  if ($match) { $s.SelectVoice($match.VoiceInfo.Name) }
}
if ($env:FRIDAY_TTS_RATE) { $s.Rate = [int]$env:FRIDAY_TTS_RATE }
$s.Speak($env:FRIDAY_TTS_TEXT)
"""

_PS_VOICES = r"""
Add-Type -AssemblyName System.Speech
(New-Object System.Speech.Synthesis.SpeechSynthesizer).GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name }
"""


class Speaker:
    """Base speaker: prints nothing, says nothing."""

    name = "none"

    def say(self, text: str) -> None:  # pragma: no cover - trivial
        pass

    def voices(self) -> list[str]:
        return []


class SilentSpeaker(Speaker):
    name = "none"


class Pyttsx3Speaker(Speaker):
    name = "pyttsx3"

    def __init__(self, voice: str = "", rate: int | None = None):
        import pyttsx3

        pyttsx3.init()  # fail early when there is no working speech driver
        self.voice = voice
        self.rate = rate

    def _engine(self):
        import pyttsx3

        # A fresh engine per utterance: re-using one hangs runAndWait() on
        # Windows after the first sentence with several pyttsx3 versions.
        engine = pyttsx3.init()
        if self.rate:
            engine.setProperty("rate", self.rate)
        if self.voice:
            wanted = self.voice.lower()
            for v in engine.getProperty("voices"):
                if wanted in (v.name or "").lower() or wanted in (v.id or "").lower():
                    engine.setProperty("voice", v.id)
                    break
        return engine

    def say(self, text: str) -> None:
        engine = self._engine()
        engine.say(text)
        engine.runAndWait()
        try:
            engine.stop()
        except Exception:  # pragma: no cover - driver specific
            pass

    def voices(self) -> list[str]:
        return [v.name for v in self._engine().getProperty("voices")]


def _sapi_rate(wpm: int) -> int:
    """Words per minute to System.Speech's -10..10 scale (0 is about 180 wpm)."""
    return max(-10, min(10, round((wpm - 180) / 20)))


def system_command(text: str, voice: str = "", rate: int | None = None) -> tuple[list[str], dict] | None:
    """The OS command (argv, extra env) that speaks ``text``, or None."""
    if sys.platform == "win32":
        exe = shutil.which("powershell") or shutil.which("pwsh")
        if not exe:
            return None
        env = {"FRIDAY_TTS_TEXT": text, "FRIDAY_TTS_VOICE": voice or ""}
        if rate:
            env["FRIDAY_TTS_RATE"] = str(_sapi_rate(rate))
        return [exe, "-NoProfile", "-NonInteractive", "-Command", _PS_SPEAK], env
    if sys.platform == "darwin":
        exe = shutil.which("say")
        if not exe:
            return None
        argv = [exe]
        if voice:
            argv += ["-v", voice]
        if rate:
            argv += ["-r", str(rate)]
        return argv + ["--", text], {}
    for name in ("espeak-ng", "espeak"):
        exe = shutil.which(name)
        if exe:
            argv = [exe]
            if voice:
                argv += ["-v", voice]
            if rate:
                argv += ["-s", str(rate)]
            return argv + ["--", text], {}
    exe = shutil.which("spd-say")
    if exe:
        return [exe, "--wait", "--", text], {}
    return None


class SystemSpeaker(Speaker):
    name = "system"

    def __init__(self, voice: str = "", rate: int | None = None):
        if system_command("test", voice, rate) is None:
            raise RuntimeError("no speech engine found on this system")
        self.voice = voice
        self.rate = rate

    def say(self, text: str) -> None:
        cmd = system_command(text, self.voice, self.rate)
        if not cmd:
            return
        argv, extra = cmd
        timeout = 30 + len(text) // 8  # long answers take a while to read out
        subprocess.run(argv, env={**os.environ, **extra}, capture_output=True, timeout=timeout, check=True)

    def voices(self) -> list[str]:
        if sys.platform != "win32":
            return []
        exe = shutil.which("powershell") or shutil.which("pwsh")
        out = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command", _PS_VOICES],
                             capture_output=True, text=True, timeout=30)
        return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def get_speaker(engine: str | None = None, voice: str | None = None, rate: int | None = None) -> Speaker:
    """Build the configured speaker, falling back to silence if none works."""
    from friday.config import config

    cfg = config.get("voice", {}) or {}
    engine = (engine or str(cfg.get("tts_engine") or "auto")).lower()
    voice = voice if voice is not None else str(cfg.get("tts_voice") or "")
    rate = rate or int(cfg.get("tts_rate") or DEFAULT_RATE)

    order = {"auto": [Pyttsx3Speaker, SystemSpeaker], "pyttsx3": [Pyttsx3Speaker],
             "system": [SystemSpeaker], "none": []}.get(engine)
    if order is None:
        raise ValueError(f"Unknown voice.tts_engine {engine!r}; use auto, pyttsx3, system or none")
    for cls in order:
        try:
            return cls(voice=voice, rate=rate)
        except Exception as exc:
            log.info("Speech engine %s unavailable: %s", cls.name, exc)
    return SilentSpeaker()


# ── Turning terminal output into something worth hearing ──────────────

_BOX = "│┃║╭╮╰╯─━═┏┓┗┛╔╗╚╝┌┐└┘├┤┬┴┼╞╡╪"
_ROW_SEP_RE = re.compile(rf"^[\s{re.escape(_BOX)}]*$")
# Spinner frames ("⠋ Thinking...") and the ASCII banner / Markdown tables.
_SKIP_RE = re.compile(r"[\u2800-\u28ff]|^\s*\+-{3,}\+\s*$|^\s*\|.*\|\s*$")
_MARK_RE = re.compile(r"^\s*(?:[✓✔✗→•!*-]|\d+\.)\s+")
_REF_RE = re.compile(r"\s?(?:#\d+|\[\d+\])(?=[\s:.,]|$)")
_RANGE_RE = re.compile(r"\b(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})\b")
_URL_RE = re.compile(r"https?://\S+")
_PATH_RE = re.compile(r"(?:[A-Za-z]:\\|/)[^\s]*[\\/][^\s]+")
_MD_RE = re.compile(r"\*\*|__|[*`>]|^#+\s*")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


def to_speech(text: str, limit: int = 320) -> str:
    """Condense printed output into a short spoken reply.

    Drops table borders, bullets, ids, links and file paths; joins the rest
    into sentences and stops at about ``limit`` characters, pointing at the
    screen for the remainder.
    """
    # Rejoin lines the terminal wrapped: an indented line continues onto
    # unindented ones until a blank line.
    lines: list[str] = []
    for raw in text.splitlines():
        if lines and lines[-1][:1].isspace() and lines[-1].strip() and raw.strip() and not raw[:1].isspace():
            lines[-1] += " " + raw
        else:
            lines.append(raw)

    parts: list[str] = []
    for raw in lines:
        # Borders, "run `friday ...`" hints and "→ step" progress lines.
        if _ROW_SEP_RE.match(raw) or _SKIP_RE.search(raw) or "`" in raw or raw.lstrip().startswith("→"):
            continue
        line = raw
        for ch in _BOX:
            line = line.replace(ch, " , ")
        line = _MARK_RE.sub("", line)
        line = _URL_RE.sub("the link", line)
        line = _PATH_RE.sub("a file", line)
        line = _REF_RE.sub("", line)
        line = _RANGE_RE.sub(r"\1 to \2", line)
        line = line.replace(" @ ", " at ")
        line = re.sub(r"Error:\s*(\w)", lambda m: "Sorry, " + m.group(1).lower(), line)
        # Sentences about command-line flags mean nothing out loud.
        line = " ".join(x for x in _SENTENCE_RE.split(line) if not re.search(r"(?:^|\s)--\w", x))
        line = _MD_RE.sub("", line)
        line = re.sub(r"\s*,\s*(?:,\s*)+", ", ", line)
        line = re.sub(r"\s+", " ", line).strip(" ,")
        if line and re.search(r"\w", line):
            parts.append(line if line[-1] in ".!?:" else line + ".")
    spoken = " ".join(parts).replace(":.", ":")
    if len(spoken) <= limit:
        return spoken
    cut = spoken[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    cut = cut[: end + 1] if end > limit // 3 else cut.rsplit(" ", 1)[0] + "..."
    return f"{cut} The rest is on screen."
