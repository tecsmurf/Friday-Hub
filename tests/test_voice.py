"""Tests for voice (friday.voice): no microphone, model or speakers needed."""

import sys

import click
import numpy as np
import pytest
from typer.testing import CliRunner

from friday.assistant.service import Assistant
from friday.assistant.store import AssistantStore
from friday.voice import listen, session, speech
from friday.voice.listen import BLOCK, ListenSettings, calibrate, is_phantom, utterances
from friday.voice.session import Conversation, clean_command, is_exit, split_wake
from friday.voice.speech import SilentSpeaker, system_command, to_speech


def silence(n):
    return [np.zeros(BLOCK, dtype=np.float32) for _ in range(n)]


def voice_(n, level):
    return [np.full(BLOCK, level, dtype=np.float32) for _ in range(n)]


class RecordingSpeaker:
    name = "fake"

    def __init__(self):
        self.said = []

    def say(self, text):
        self.said.append(text)


# ── speech output ──────────────────────────────────────────────────────


class TestToSpeech:
    def test_strips_marks_ids_and_hints(self):
        printed = (
            "  ✓ Booked #4 Dentist at Main St: tomorrow 15:00-15:45\n"
            "  Reminder set for tomorrow 14:45.\n"
            "  Reminders pop up while `friday daemon run` is running (`friday daemon install`\n"
            "starts it at log-on).\n"
        )
        assert to_speech(printed) == ("Booked Dentist at Main St: tomorrow 15:00 to 15:45. "
                                      "Reminder set for tomorrow 14:45.")

    def test_errors_become_apologies_without_cli_flags(self):
        printed = ("\n  Error: That time overlaps Dentist (tomorrow 15:00). Next free slot: tomorrow 15:45. "
                   "Use --force to double-book, or pick another time.\n")
        assert to_speech(printed) == ("Sorry, that time overlaps Dentist (tomorrow 15:00). "
                                      "Next free slot: tomorrow 15:45.")

    def test_tables_links_and_paths(self):
        printed = (
            "╭───┬──────────╮\n│ # │ Todo     │\n├───┼──────────┤\n│ 1 │ buy milk │\n╰───┴──────────╯\n"
            "  → Searching: monitors\n"
            "  ✓ Saved research report to /home/me/.friday/data/research/monitors.md\n"
            "See https://example.com/page for more\n"
        )
        spoken = to_speech(printed)
        assert "│" not in spoken and "─" not in spoken and "Searching" not in spoken
        assert "buy milk" in spoken and "a file" in spoken and "the link" in spoken
        assert "/home" not in spoken and "https" not in spoken

    def test_long_output_is_cut_at_a_sentence(self):
        printed = "\n".join(f"Sentence number {i} is here." for i in range(50))
        spoken = to_speech(printed, limit=120)
        assert spoken.endswith("The rest is on screen.")
        assert len(spoken) < 160
        assert "Sentence number 0 is here." in spoken

    def test_skips_spinners_and_banner(self):
        printed = ("\n  +-------------+\n  |  FRIDAY     |\n  +-------------+\n\n"
                   "⠋ Hermes is thinking...⠙ Hermes is thinking...\nTask: x\nAll tests pass.\n")
        assert to_speech(printed) == "Task: x. All tests pass."

    def test_empty(self):
        assert to_speech("\n  \n╭──╮\n") == ""


class TestSystemSpeech:
    def test_windows_passes_text_in_env_not_script(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(speech.shutil, "which", lambda name: "C:/ps.exe" if name == "powershell" else None)
        text = "'; Remove-Item C:\\ -Recurse; '"
        argv, env = system_command(text, voice="Zira", rate=220)
        assert text not in " ".join(argv)
        assert env["FRIDAY_TTS_TEXT"] == text and env["FRIDAY_TTS_VOICE"] == "Zira"
        assert env["FRIDAY_TTS_RATE"] == "2"

    def test_macos_and_linux(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(speech.shutil, "which", lambda name: f"/usr/bin/{name}")
        argv, _ = system_command("-hello", rate=200)
        assert argv[0].endswith("say") and argv[-2:] == ["--", "-hello"]

        monkeypatch.setattr(sys, "platform", "linux")
        argv, _ = system_command("hi")
        assert argv[0].endswith("espeak-ng") and argv[-1] == "hi"

        monkeypatch.setattr(speech.shutil, "which", lambda name: None)
        assert system_command("hi") is None

    def test_sapi_rate_is_clamped(self):
        assert speech._sapi_rate(180) == 0
        assert speech._sapi_rate(1000) == 10 and speech._sapi_rate(-500) == -10

    def test_get_speaker_falls_back_to_silence(self, monkeypatch):
        def broken(*a, **k):
            raise RuntimeError("no driver")

        monkeypatch.setattr(speech.Pyttsx3Speaker, "__init__", broken)
        monkeypatch.setattr(speech.SystemSpeaker, "__init__", broken)
        assert isinstance(speech.get_speaker("auto"), SilentSpeaker)
        assert speech.get_speaker("none").name == "none"
        with pytest.raises(ValueError):
            speech.get_speaker("klingon")


# ── listening ──────────────────────────────────────────────────────────


class TestUtterances:
    settings = ListenSettings(threshold=0.05, silence_seconds=0.3, max_seconds=2.0, min_seconds=0.1,
                              start_timeout=0.6)

    def test_splits_on_silence_and_keeps_pre_roll(self):
        blocks = silence(20) + voice_(15, 0.2) + silence(20) + voice_(10, 0.4) + silence(20)
        found = list(utterances(blocks, self.settings))
        assert len(found) == 2
        assert np.isclose(found[0].max(), 0.2) and np.isclose(found[1].max(), 0.4)
        assert found[0][0] == 0.0  # starts with a little silence: the first syllable isn't clipped

    def test_ignores_clicks(self):
        assert list(utterances(silence(10) + voice_(1, 0.9) + silence(20), self.settings)) == []

    def test_caps_long_speech(self):
        found = list(utterances(voice_(200, 0.2), self.settings))
        assert len(found) >= 2
        assert all(len(u) <= int(2.0 / 0.03) * BLOCK + BLOCK for u in found)

    def test_once_stops_after_first_or_on_timeout(self):
        blocks = voice_(10, 0.2) + silence(20) + voice_(10, 0.3) + silence(20)
        assert len(list(utterances(blocks, self.settings, once=True))) == 1
        assert list(utterances(silence(100), self.settings, once=True)) == []

    def test_calibrate_sits_above_room_noise(self):
        rng = np.random.default_rng(0)
        noisy = [rng.normal(0, 0.01, BLOCK).astype(np.float32) for _ in range(40)]
        threshold = calibrate(iter(noisy))
        assert 0.02 < threshold < 0.06
        assert calibrate(iter(silence(40))) == pytest.approx(0.012)

    @pytest.mark.parametrize("text,phantom", [
        ("", True), ("Thank you.", True), (" you ", True), ("Thanks for watching!", True),
        ("Thank you, remind me to call mom", False), ("what's on today", False),
    ])
    def test_phantoms(self, text, phantom):
        assert is_phantom(text) is phantom

    def test_missing_packages_explain_how_to_install(self, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def no_audio(name, *args, **kwargs):
            if name in ("sounddevice", "faster_whisper"):
                raise ImportError(f"No module named {name}")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", no_audio)
        with pytest.raises(listen.VoiceUnavailable, match=r"\.\[voice\]"):
            listen.Microphone()
        with pytest.raises(listen.VoiceUnavailable, match=r"\.\[voice\]"):
            listen.Transcriber().load()


# ── wake word and commands ─────────────────────────────────────────────


class TestWakeWord:
    @pytest.mark.parametrize("heard,wanted", [
        ("Friday, what's on today?", "what's on today"),
        ("Hey Friday remind me to stretch in 10 minutes.", "remind me to stretch in 10 minutes"),
        ("OK, Friday. Add a todo, buy milk", "Add a todo, buy milk"),
        ("Fri day, show my todos", "show my todos"),
        ("Friday.", ""),
        ("friday please book lunch tomorrow at noon", "book lunch tomorrow at noon"),
    ])
    def test_wakes(self, heard, wanted):
        assert split_wake(heard) == (True, wanted)

    @pytest.mark.parametrize("heard", [
        "Move my meeting to Friday", "I'll see you on Friday", "Fridge is empty", "", "Thank you.",
    ])
    def test_stays_asleep(self, heard):
        assert split_wake(heard)[0] is False

    def test_custom_wake_word(self):
        assert split_wake("Jarvis, lights on", "jarvis") == (True, "lights on")
        assert split_wake("Friday, lights on", "jarvis")[0] is False

    def test_clean_command_and_exit(self):
        assert clean_command("  Can you remind me to call Mom at 6 p.m.?  ") == "remind me to call Mom at 6pm"
        assert clean_command("Book lunch at 12:30 P.M. tomorrow") == "Book lunch at 12:30pm tomorrow"
        assert clean_command("add milk to my list, please.") == "add milk to my list"
        for phrase in ("Goodbye.", "OK, bye Friday", "That's all, thanks", "stop listening", "Exit"):
            assert is_exit(phrase), phrase
        assert not is_exit("say goodbye to Bob at 5pm")


# ── conversations ──────────────────────────────────────────────────────


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    from friday.assistant import cli as acli

    store = AssistantStore(tmp_path / "voice.db")
    monkeypatch.setattr(acli, "_assistant", lambda: Assistant(store, calendar=None))
    yield store
    store.close()


class TestConversation:
    def test_session_flow(self):
        speaker = RecordingSpeaker()
        handled = []

        def handler(text, say=None):
            handled.append(text)
            if text == "explode":
                raise RuntimeError("boom")
            return f"Done: {text}"

        convo = Conversation(speaker, handler=handler)
        convo.run(iter(["", "Remind me to stretch.", "never mind", "explode", "Okay, goodbye", "not reached"]))
        assert handled == ["Remind me to stretch", "explode"]
        assert speaker.said == ["Done: Remind me to stretch", "Okay.", "Sorry, something went wrong: boom",
                                "Goodbye."]

    def test_quiet_mode_never_speaks(self):
        speaker = RecordingSpeaker()
        Conversation(speaker, say_out_loud=False, handler=lambda t, say=None: "ok").run(iter(["hi", "bye"]))
        assert speaker.said == []

    def test_broken_speaker_does_not_end_session(self):
        class Broken:
            def say(self, text):
                raise OSError("no audio device")

        handled = []
        convo = Conversation(Broken(), handler=lambda t, say=None: handled.append(t) or "ok")
        convo.run(iter(["one", "two", "bye"]))
        assert handled == ["one", "two"]

    def test_handle_runs_real_requests(self, isolated):
        said = []
        reply = session.handle("remind me to stretch in 30 minutes", say=said.append)
        assert "stretch" in reply and "`" not in reply and "daemon" not in reply
        assert said == []  # quick actions don't announce themselves

        assert "Added todo" in session.handle("add todo buy milk")
        assert session.handle("show my todos") == "You have 1 todo: buy milk."

        assert "Booked" in session.handle("book dentist tomorrow at 3pm")
        clash = session.handle("book lunch tomorrow at 3pm")
        assert clash.startswith("Sorry, that time overlaps") and "--force" not in clash

        agenda = session.handle("what's on tomorrow")
        assert "Dentist" in agenda and "15:00 to 15:30" in agenda

    def test_slow_requests_are_announced(self, isolated, monkeypatch):
        from friday.assistant.intent import Intent

        async def understand(text, use_llm=True):
            return Intent("research", title=text)

        said = []
        reply = session.handle("research monitors", understand=understand,
                               run_intent=lambda intent, text: print("unused"), say=said.append)
        assert said == ["On it. This may take a minute."]
        assert reply == "Done."


class FakeMic:
    """Replays prepared audio blocks instead of a microphone."""

    def __init__(self, blocks):
        self._blocks = iter(blocks)  # shared, like the real microphone's queue
        self.muted = False
        self.flushes = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def flush(self):
        self.flushes += 1

    def blocks(self):
        yield from self._blocks

    @staticmethod
    def devices():
        return ["0: Fake mic"]


class FakeTranscriber:
    """Each utterance is a constant level; the level picks the words."""

    model_name = "fake"

    def __init__(self, words):
        self.words = words

    def load(self):
        return self

    def transcribe(self, audio):
        return self.words[round(float(audio.max()), 1)]


class TestVoiceCli:
    def _install(self, monkeypatch, blocks, words, handled):
        from friday.voice import cli as vcli

        monkeypatch.setattr(listen, "Microphone", lambda *a, **k: FakeMic(blocks))
        monkeypatch.setattr(listen, "Transcriber", lambda *a, **k: FakeTranscriber(words))
        monkeypatch.setattr(session, "handle", lambda text, say=None: handled.append(text) or "Okay then.")
        monkeypatch.setattr(speech, "get_speaker", lambda *a, **k: RecordingSpeaker())
        return vcli

    def test_hands_free_wake_word(self, monkeypatch):
        from friday.cli import app

        handled = []
        blocks = (silence(30)
                  + voice_(15, 0.1) + silence(40)   # "Move it to Friday" -> ignored
                  + voice_(15, 0.2) + silence(40)   # "Friday, what's on today?"
                  + voice_(15, 0.3) + silence(40)   # "Friday." -> "Yes?"
                  + voice_(15, 0.4) + silence(40)   # "add a todo buy milk"
                  + voice_(15, 0.5) + silence(40))  # "Friday, goodbye"
        words = {0.1: "Move it to Friday.", 0.2: "Friday, what's on today?", 0.3: "Friday.",
                 0.4: "Add a todo, buy milk.", 0.5: "Friday, goodbye."}
        self._install(monkeypatch, blocks, words, handled)
        result = CliRunner().invoke(app, ["voice", "--wake"])
        assert result.exit_code == 0, result.output
        assert handled == ["what's on today", "Add a todo, buy milk"]
        assert "Yes?" in result.output and "Goodbye." in result.output
        assert "(heard: Move it to Friday.)" in result.output

    def test_push_to_talk_and_typing(self, monkeypatch):
        from friday.cli import app

        handled = []
        blocks = silence(30) + voice_(15, 0.2) + silence(40)
        self._install(monkeypatch, blocks, {0.2: "Remind me to stretch in 5 minutes."}, handled)
        result = CliRunner().invoke(app, ["voice"], input="\nshow my todos\nq\n")
        assert result.exit_code == 0, result.output
        assert handled == ["Remind me to stretch in 5 minutes", "show my todos"]

    def test_type_mode_needs_no_audio_packages(self, monkeypatch):
        from friday.cli import app

        handled = []
        self._install(monkeypatch, [], {}, handled)

        def no_mic(*a, **k):
            raise AssertionError("typed mode must not open the microphone")

        monkeypatch.setattr(listen, "Microphone", no_mic)
        result = CliRunner().invoke(app, ["voice", "--type", "--quiet"], input="add todo call bank\nbye\n")
        assert result.exit_code == 0, result.output
        assert handled == ["add todo call bank"]

    def test_missing_voice_packages_fail_helpfully(self, monkeypatch):
        from friday.cli import app

        class NoModel:
            model_name = "base.en"

            def load(self):
                raise listen.VoiceUnavailable('Speech recognition isn\'t installed. Run: pip install -e ".[voice]"')

        monkeypatch.setattr(listen, "Transcriber", lambda *a, **k: NoModel())
        monkeypatch.setattr(speech, "get_speaker", lambda *a, **k: SilentSpeaker())
        result = CliRunner().invoke(app, ["voice"])
        assert result.exit_code == 1
        output = click.unstyle(result.output)
        assert '".[voice]"' in output and "friday voice --type" in output

    def test_say_and_status(self, monkeypatch):
        from friday.cli import app

        spoken = RecordingSpeaker()
        monkeypatch.setattr(speech, "get_speaker", lambda *a, **k: spoken)
        monkeypatch.setattr(listen, "Microphone", FakeMic)
        assert CliRunner().invoke(app, ["voice", "say", "hello", "there"]).exit_code == 0
        assert spoken.said == ["hello there"]
        status = CliRunner().invoke(app, ["voice", "status"])
        assert status.exit_code == 0 and "Wake word" in status.output and "Fake mic" in status.output


def test_notifications_can_be_spoken(monkeypatch, tmp_path):
    from friday.assistant import notify as notify_mod
    from friday.config import config

    spoken = RecordingSpeaker()
    monkeypatch.setattr(speech, "get_speaker", lambda *a, **k: spoken)
    monkeypatch.setattr(notify_mod, "_popup", lambda title, body: True)
    monkeypatch.setitem(config, "voice", {**config.get("voice", {}), "speak_notifications": False})
    notify_mod.notify("Friday reminder", "Call mom", log_path=tmp_path / "n.log")
    assert spoken.said == []

    monkeypatch.setitem(config, "voice", {**config.get("voice", {}), "speak_notifications": True})
    assert notify_mod.notify("Friday reminder", "Call mom", log_path=tmp_path / "n.log") is True
    notify_mod.notify("Friday reminder", "Quiet", desktop=False, log_path=tmp_path / "n.log")
    assert spoken.said == ["Reminder: Call mom"]


@pytest.mark.parametrize("said,kind", [
    ("what's on tomorrow", "agenda"), ("what do I have on friday", "agenda"), ("am I free this afternoon", "agenda"),
    ("what's on reddit", None),
])
def test_spoken_agenda_questions(said, kind):
    from friday.assistant.intent import parse_rules

    intent = parse_rules(said)
    assert (intent.kind if intent else None) == kind


# ── Real speech round trip (CI only: downloads a Whisper model) ────────


def _synthesize(text: str, path) -> bool:
    """Speak ``text`` into a WAV file with the OS voice. False if impossible."""
    import os
    import shutil
    import subprocess

    if sys.platform == "win32":
        exe = shutil.which("powershell") or shutil.which("pwsh")
        script = ("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                  "$s.SetOutputToWaveFile($env:WAV); $s.Speak($env:TXT); $s.Dispose()")
        cmd = [exe, "-NoProfile", "-NonInteractive", "-Command", script] if exe else None
    else:
        exe = shutil.which("espeak-ng") or shutil.which("espeak")
        cmd = [exe, "-v", "en-us", "-s", "135", "-g", "4", "-w", str(path), "--", text] if exe else None
    if not cmd:
        return False
    subprocess.run(cmd, env={**os.environ, "WAV": str(path), "TXT": text}, check=True, timeout=60)
    return True


def _read_wav_16k(path) -> np.ndarray:
    import wave

    with wave.open(str(path), "rb") as wav:
        rate, width, channels = wav.getframerate(), wav.getsampwidth(), wav.getnchannels()
        frames = wav.readframes(wav.getnframes())
    assert width == 2, "expected 16-bit PCM"
    audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    audio = audio.reshape(-1, channels).mean(axis=1)
    positions = np.arange(0, len(audio), rate / listen.SAMPLE_RATE)
    return np.interp(positions, np.arange(len(audio)), audio).astype(np.float32)


@pytest.mark.skipif(not __import__("os").environ.get("FRIDAY_VOICE_E2E"),
                    reason="set FRIDAY_VOICE_E2E=1 to run (needs the voice extra and downloads a model)")
def test_real_speech_round_trip(tmp_path):
    """The OS voice says a request; Whisper hears it; Friday understands it."""
    from friday.assistant.intent import parse_rules

    wav = tmp_path / "request.wav"
    if not _synthesize("Friday, remind me to call mom at 6 pm.", wav):
        pytest.skip("no speech synthesizer on this machine")
    audio = _read_wav_16k(wav)

    # The level detector finds the spoken request inside silence.
    padded = np.concatenate([np.zeros(listen.SAMPLE_RATE, np.float32), audio, np.zeros(listen.SAMPLE_RATE, np.float32)])
    blocks = [padded[i:i + BLOCK] for i in range(0, len(padded) - BLOCK, BLOCK)]
    found = list(utterances(blocks, ListenSettings(threshold=0.02, silence_seconds=0.9, max_seconds=15)))
    assert found, "no utterance detected"

    # The default model, as users get it; tiny.en mishears the robotic espeak voice.
    heard = listen.Transcriber(model="base.en").transcribe(np.concatenate(found))
    woke, command = split_wake(heard)
    assert woke, heard
    intent = parse_rules(clean_command(command))
    assert intent and intent.kind == "remind", heard
    assert "mom" in intent.title.lower() and intent.when.hour == 18, heard


def test_broken_sound_system_is_reported_not_crashed(monkeypatch):
    """sounddevice raises PortAudioError on import when no sound server runs."""

    class PortAudioError(Exception):
        pass

    import builtins

    real_import = builtins.__import__

    def broken(name, *args, **kwargs):
        if name == "sounddevice":
            raise PortAudioError("Error initializing PortAudio: Can't connect to server")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", broken)
    assert listen.Microphone.devices() == []
    with pytest.raises(listen.VoiceUnavailable, match="PortAudio"):
        listen.Microphone()
