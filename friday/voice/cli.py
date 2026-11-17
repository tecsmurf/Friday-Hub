"""
Voice CLI: ``friday voice`` (talk to Friday), ``friday voice say``,
``friday voice status`` and ``friday voice setup``.
"""

from typing import Iterator

import typer
from rich.markup import escape
from rich.text import Text

from friday.display import console, show_error, show_info

voice_app = typer.Typer(
    help="Talk to Friday: speak a request and hear the answer (offline speech, optional wake word).",
    invoke_without_command=True,
)


def _fail(message: str, code: int = 1):
    show_error(message)
    raise typer.Exit(code)


def _ok(message: str) -> None:
    console.print(Text("  ✓ ", style="bold green") + Text(message))


def _settings():
    from friday.voice.listen import ListenSettings

    return ListenSettings.from_config()


def _wake_word() -> str:
    from friday.config import config

    return str((config.get("voice", {}) or {}).get("wake_word") or "friday")


def _calibrated(mic, settings):
    from friday.voice.listen import calibrate

    if not settings.threshold:
        console.print(Text("  Measuring background noise, stay quiet for a moment...", style="dim"))
        settings.threshold = calibrate(mic.blocks())
    return settings


def _hear(mic, transcriber, settings, once=True) -> Iterator[str]:
    from friday.voice.listen import utterances

    for audio in utterances(mic.blocks(), settings, once=once):
        with console.status("[bright_blue]Understanding...[/]", spinner="dots"):
            text = transcriber.transcribe(audio)
        if text:
            yield text
        elif once:
            yield ""


def _push_to_talk(conversation, transcriber, typed_only: bool) -> Iterator[str]:
    """Enter = speak; any typed text is used as the request; q quits."""
    from friday.voice.listen import Microphone

    hint = "Type a request (q to quit)" if typed_only else "Press Enter and speak, or type a request (q to quit)"
    mic = None if typed_only else Microphone()
    settings = _settings()
    if mic:  # measure the room now, not after Enter when you're already talking
        with mic:
            _calibrated(mic, settings)
    while True:
        try:
            typed = console.input(f"\n[bright_cyan]{hint}:[/] ").strip()
        except EOFError:
            return
        if typed.lower() in ("q", "quit", "exit"):
            conversation.reply("Goodbye.")
            return
        if typed or typed_only:
            yield typed
            continue
        with mic:
            mic.flush()
            console.print(Text("  Listening...", style="bold bright_blue"))
            heard = next(_hear(mic, transcriber, settings, once=True), "")
        if not heard:
            console.print(Text("  I didn't catch anything.", style="yellow"))
            continue
        yield heard


def _hands_free(conversation, transcriber, speaker) -> Iterator[str]:
    """Listen continuously; act on sentences that start with the wake word."""
    from friday.voice.listen import Microphone
    from friday.voice.session import split_wake

    wake = _wake_word()
    settings = _settings()
    mic = Microphone()

    # Don't let Friday hear its own voice.
    original_say = speaker.say

    def say_muted(text: str) -> None:
        mic.muted = True
        try:
            original_say(text)
        finally:
            mic.flush()
            mic.muted = False

    speaker.say = say_muted

    with mic:
        _calibrated(mic, settings)
        console.print(Text(f'  Listening for "{wake.title()}"... (Ctrl+C to stop)', style="bold bright_blue"))
        for text in _hear(mic, transcriber, settings, once=False):
            woke, request = split_wake(text, wake)
            if not woke:
                console.print(Text(f"  (heard: {text})", style="dim"))
                continue
            if not request:
                conversation.reply("Yes?")
                request = next(_hear(mic, transcriber, settings, once=True), "")
                if not request:
                    continue
            mic.muted = True  # ignore the room while Friday works on it
            try:
                yield request
            finally:
                mic.flush()
                mic.muted = False
            console.print(Text(f'  Listening for "{wake.title()}"...', style="dim"))


@voice_app.callback()
def voice(
    ctx: typer.Context,
    wake: bool = typer.Option(False, "--wake", "-w", help='Hands-free: respond whenever you say "Friday, ..."'),
    typed: bool = typer.Option(False, "--type", "-t", help="Type requests instead of speaking (replies are spoken)"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Don't speak replies, only print them"),
):
    """Talk to Friday. Press Enter and speak; Friday answers out loud."""
    if ctx.invoked_subcommand is not None:
        return
    from friday.voice.listen import Transcriber, VoiceUnavailable
    from friday.voice.session import Conversation
    from friday.voice.speech import SilentSpeaker, get_speaker

    if wake and typed:
        _fail("Choose either --wake or --type.", code=2)
    speaker = SilentSpeaker() if quiet else get_speaker()
    if not quiet and speaker.name == "none":
        show_info("No speech engine found, so replies are printed only. See `friday voice status`.")
    conversation = Conversation(speaker, say_out_loud=not quiet)
    transcriber = None if typed else Transcriber()

    try:
        if transcriber:
            with console.status(f"[bright_blue]Loading speech model ({transcriber.model_name})...[/]"):
                transcriber.load()
        requests = (_hands_free(conversation, transcriber, speaker) if wake
                    else _push_to_talk(conversation, transcriber, typed_only=typed))
        conversation.reply("Hi, I'm listening." if not typed else "Hi, what can I do for you?")
        conversation.run(requests)
    except VoiceUnavailable as exc:
        _fail(f"{exc}\n  You can still type to Friday with: friday voice --type")
    except KeyboardInterrupt:
        console.print()
        conversation.reply("Goodbye.")


@voice_app.command("say")
def say_cmd(text: list[str] = typer.Argument(..., help="What Friday should say")):
    """Say something out loud (checks that speech output works)."""
    from friday.voice.speech import get_speaker

    speaker = get_speaker()
    if speaker.name == "none":
        _fail("No speech engine found. Install one with: pip install -e \".[voice]\"")
    try:
        speaker.say(" ".join(text))
    except Exception as exc:
        _fail(f"Speech failed ({speaker.name}): {exc}")


@voice_app.command("status")
def status_cmd():
    """Show which speech engines, model and microphones Friday will use."""
    import importlib.util

    from friday.config import config
    from friday.voice.listen import Microphone
    from friday.voice.speech import get_speaker

    cfg = config.get("voice", {}) or {}

    def have(module: str) -> str:
        return "[green]installed[/]" if importlib.util.find_spec(module) else "[red]missing[/]"

    console.print("[bold bright_cyan]Voice[/]\n")
    console.print(f"  [cyan]Speech output:[/] {escape(get_speaker().name)} (setting: {escape(str(cfg.get('tts_engine', 'auto')))})")
    console.print(f"  [cyan]Speech recognition:[/] faster-whisper {have('faster_whisper')}, "
                  f"model {escape(str(cfg.get('stt_model', 'base.en')))}")
    console.print(f"  [cyan]Microphone support:[/] sounddevice {have('sounddevice')}")
    console.print(f"  [cyan]Wake word:[/] {escape(_wake_word())}")
    mics = Microphone.devices()
    if mics:
        console.print("  [cyan]Microphones:[/]")
        for name in mics:
            console.print(f"    {escape(name)}")
    if not importlib.util.find_spec("faster_whisper") or not importlib.util.find_spec("sounddevice"):
        show_info('Install voice support with: pip install -e ".[voice]"  then run: friday voice setup')


@voice_app.command("setup")
def setup_cmd(
    seconds: float = typer.Option(3.0, "--seconds", help="How long to test the microphone"),
):
    """Download the speech model, test the microphone and the voice."""
    import numpy as np

    from friday.voice.listen import Microphone, Transcriber, VoiceUnavailable, calibrate, rms
    from friday.voice.speech import get_speaker

    try:
        transcriber = Transcriber()
        with console.status(f"[bright_blue]Downloading/loading speech model {transcriber.model_name}...[/]"):
            transcriber.load()
        _ok(f"Speech model {transcriber.model_name} is ready (works offline from now on).")

        mic = Microphone()
        with mic:
            noise = calibrate(mic.blocks())
            console.print(Text(f"  Say something for {seconds:.0f} seconds...", style="bold bright_blue"))
            mic.flush()
            blocks = []
            for block in mic.blocks():
                blocks.append(block)
                if len(blocks) * len(block) >= seconds * 16_000:
                    break
        audio = np.concatenate(blocks)
        peak = max(rms(b) for b in blocks)
        if peak < noise:
            show_info("That sounded like silence. Check the microphone, or pick one with voice.mic_device "
                      "(see `friday voice status`).")
        else:
            _ok("Microphone works.")
            heard = transcriber.transcribe(audio)
            show_info(f"I heard: {heard or '(nothing clear)'}")
    except VoiceUnavailable as exc:
        _fail(str(exc))

    speaker = get_speaker()
    if speaker.name == "none":
        show_info("No speech engine found, so Friday will reply in text only.")
        return
    speaker.say("Voice is set up. Say Friday, then what you need.")
    _ok(f"Speech output works ({speaker.name}). Try: friday voice")


def register(app: typer.Typer) -> None:
    app.add_typer(voice_app, name="voice")
