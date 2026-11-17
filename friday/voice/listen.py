"""
Speech-to-text: Friday listens.

Audio comes from the microphone through ``sounddevice`` (16 kHz mono) and is
cut into utterances with a simple energy detector: an utterance starts when
the level rises above a threshold calibrated from the room's background noise
and ends after a short silence. Each utterance is transcribed locally by
``faster-whisper``. Nothing leaves the computer; the Whisper model is
downloaded once on first use (or by ``friday voice setup``) and cached.
"""

import logging
import queue
from dataclasses import dataclass
from typing import Iterable, Iterator

import numpy as np

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
BLOCK_SECONDS = 0.03
BLOCK = int(SAMPLE_RATE * BLOCK_SECONDS)

# Phrases Whisper is known to invent from silence or background noise.
_PHANTOMS = {"", "you", "thank you", "thanks", "thank you for watching", "thanks for watching", "bye",
             "please subscribe", "so", "okay", "uh", "um", "hmm", "music", "applause", "silence"}


class VoiceUnavailable(RuntimeError):
    """Speech input can't run: a package, model or microphone is missing."""


@dataclass
class ListenSettings:
    threshold: float = 0.0        # RMS level that counts as speech; 0 = calibrate
    silence_seconds: float = 0.9  # pause that ends an utterance
    max_seconds: float = 15.0     # hard cap on one utterance
    min_seconds: float = 0.3      # shorter blips (clicks, coughs) are ignored
    start_timeout: float = 8.0    # give up when nobody starts talking (push-to-talk)

    @classmethod
    def from_config(cls) -> "ListenSettings":
        from friday.config import config

        cfg = config.get("voice", {}) or {}
        return cls(
            threshold=float(cfg.get("energy_threshold") or 0.0),
            silence_seconds=float(cfg.get("silence_seconds") or 0.9),
            max_seconds=float(cfg.get("max_record_seconds") or 15.0),
            start_timeout=float(cfg.get("listen_timeout_seconds") or 8.0),
        )


def rms(block: np.ndarray) -> float:
    if block.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(block.astype(np.float32)))))


def calibrate(blocks: Iterable[np.ndarray], seconds: float = 0.6) -> float:
    """Speech threshold from a moment of background noise."""
    levels = []
    for block in blocks:
        levels.append(rms(block))
        if len(levels) * BLOCK_SECONDS >= seconds:
            break
    noise = float(np.percentile(levels, 90)) if levels else 0.0
    return max(0.012, noise * 3.0)


def utterances(blocks: Iterable[np.ndarray], settings: ListenSettings, once: bool = False) -> Iterator[np.ndarray]:
    """Cut a stream of audio blocks into utterances.

    A little audio from just before the level rose is kept so the first
    syllable isn't clipped. With ``once``, stops after one utterance or when
    nobody speaks within ``settings.start_timeout`` seconds.
    """
    pre_roll = max(1, int(0.3 / BLOCK_SECONDS))
    silence_blocks = max(1, int(settings.silence_seconds / BLOCK_SECONDS))
    max_blocks = int(settings.max_seconds / BLOCK_SECONDS)
    min_blocks = int(settings.min_seconds / BLOCK_SECONDS)
    timeout_blocks = int(settings.start_timeout / BLOCK_SECONDS)

    history: list[np.ndarray] = []
    current: list[np.ndarray] | None = None
    voiced = quiet = waited = 0
    for block in blocks:
        loud = rms(block) >= settings.threshold
        if current is None:
            history = (history + [block])[-pre_roll:]
            waited += 1
            if loud:
                current, voiced, quiet = list(history), 1, 0
            elif once and waited >= timeout_blocks:
                return
            continue
        current.append(block)
        if loud:
            voiced += 1
            quiet = 0
        else:
            quiet += 1
        if quiet >= silence_blocks or len(current) >= max_blocks:
            if voiced >= min_blocks:
                yield np.concatenate(current).astype(np.float32)
                if once:
                    return
            current, history, waited = None, [], 0
    if current is not None and voiced >= min_blocks:
        yield np.concatenate(current).astype(np.float32)


class Microphone:
    """The default (or configured) input device as a stream of 30 ms blocks."""

    def __init__(self, device: str | int | None = None):
        try:
            import sounddevice as sd
        except (ImportError, OSError) as exc:
            raise VoiceUnavailable(
                f"Microphone support isn't installed ({exc}). Run: pip install -e \".[voice]\"") from exc
        if device in ("", None):
            from friday.config import config

            device = (config.get("voice", {}) or {}).get("mic_device") or None
        if isinstance(device, str) and device.isdigit():
            device = int(device)
        self._sd = sd
        self.device = device
        self._queue: queue.Queue = queue.Queue()
        self._stream = None
        self.muted = False

    def _callback(self, indata, frames, time_info, status):  # runs on the audio thread
        if status:
            log.debug("audio status: %s", status)
        if not self.muted:
            self._queue.put(indata[:, 0].copy())

    def __enter__(self) -> "Microphone":
        try:
            self._stream = self._sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                                                blocksize=BLOCK, device=self.device, callback=self._callback)
            self._stream.start()
        except Exception as exc:
            raise VoiceUnavailable(f"Couldn't open the microphone: {exc}") from exc
        return self

    def __exit__(self, *exc) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def flush(self) -> None:
        """Drop audio captured so far (e.g. Friday's own voice)."""
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def blocks(self) -> Iterator[np.ndarray]:
        while True:
            yield self._queue.get()

    @staticmethod
    def devices() -> list[str]:
        try:
            import sounddevice as sd
        except (ImportError, OSError):
            return []
        names = []
        for index, dev in enumerate(sd.query_devices()):
            if dev.get("max_input_channels", 0) > 0:
                names.append(f"{index}: {dev['name']}")
        return names


class Transcriber:
    """faster-whisper, loaded once on first use."""

    def __init__(self, model: str | None = None, language: str | None = None):
        from friday.config import config

        cfg = config.get("voice", {}) or {}
        self.model_name = model or str(cfg.get("stt_model") or "base.en")
        self.language = language or str(cfg.get("language") or "en")
        self.device = str(cfg.get("stt_device") or "cpu")
        self.compute_type = str(cfg.get("stt_compute_type") or "int8")
        self._model = None

    def load(self):
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise VoiceUnavailable(
                    "Speech recognition isn't installed. Run: pip install -e \".[voice]\"") from exc
            try:
                self._model = WhisperModel(self.model_name, device=self.device, compute_type=self.compute_type)
            except Exception as exc:
                raise VoiceUnavailable(
                    f"Couldn't load the speech model '{self.model_name}': {exc}. The first run downloads it, "
                    "so check your internet connection (or run `friday voice setup`).") from exc
        return self._model

    def transcribe(self, audio: np.ndarray) -> str:
        model = self.load()
        segments, _info = model.transcribe(
            audio, language=self.language or None, beam_size=5, condition_on_previous_text=False,
            vad_filter=False)
        text = " ".join(s.text.strip() for s in segments).strip()
        return "" if is_phantom(text) else text


def is_phantom(text: str) -> bool:
    """True for empty text or a phrase Whisper hallucinates from noise."""
    import re

    words = re.sub(r"[^\w' ]+", " ", text.lower())
    words = re.sub(r"\s+", " ", words).strip()
    return words in _PHANTOMS
