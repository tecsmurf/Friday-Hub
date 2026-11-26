"""
Voice for Friday: speak requests and hear the answers.

Everything runs locally and offline once set up:

* speech-to-text: faster-whisper (optional ``voice`` extra) on audio from the
  microphone via sounddevice;
* text-to-speech: pyttsx3 when installed, otherwise the speech engine built
  into the OS (Windows System.Speech, macOS ``say``, Linux espeak);
* an optional wake word ("Friday") for hands-free use.
"""
