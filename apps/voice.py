"""
PyOS NOVA — Voice Interface
=============================
Speak commands to NOVA. Fully offline.

Speech-to-text: whisper.cpp (via pywhisper or faster-whisper)
Text-to-speech: pyttsx3 (system TTS — no cloud)
Wake word:      keyword match (no pvporcupine needed for basic use)

Usage:
  voice start         — start listening (background)
  voice stop          — stop
  voice say <text>    — speak text
  voice test          — test TTS/STT pipeline
  voice status        — show voice status

Requires: pip install pyttsx3 openai-whisper sounddevice numpy
Optional:  pip install faster-whisper  (much faster on CPU)
"""

import os, sys, threading, queue, time
from typing import Optional, Callable, TYPE_CHECKING

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)

if TYPE_CHECKING:
    from kernel.nova import NovaKernel

WAKE_WORDS = ["nova", "hey nova", "computer", "assistant"]
SAMPLE_RATE = 16000
CHUNK_DURATION = 3   # seconds per audio chunk


class TTSEngine:
    """Text-to-speech using pyttsx3 (offline, system TTS)."""

    def __init__(self):
        """Initialise the instance."""
        self._engine = None
        self._ready  = False
        self._lock   = threading.Lock()

    def init(self) -> bool:
        """Initialise the operation.


            Returns:
                bool: Result.
            """
        try:
            import pyttsx3
            self._engine = pyttsx3.init()
            self._engine.setProperty("rate", 175)
            self._engine.setProperty("volume", 0.9)
            self._ready = True
            return True
        except Exception:
            return False

    @property
    def available(self) -> bool:
        """Available.


            Returns:
                bool: Result.
            """
        if not self._ready:
            self.init()
        return self._ready

    def say(self, text: str):
        """Speak the operation aloud via TTS.

            Args:
            text (str): Text.
            """
        if not self.available:
            print(f"  [Voice] {text}")
            return
        with self._lock:
            try:
                self._engine.say(text)
                self._engine.runAndWait()
            except Exception as e:
                print(f"  [TTS error: {e}]")

    def voices(self) -> list:
        """Voices.


            Returns:
                list: Result.
            """
        if not self.available: return []
        try:
            return [v.name for v in self._engine.getProperty("voices")]
        except Exception: return []


class STTEngine:
    """
    Speech-to-text using Whisper (offline).
    Tries: faster-whisper (fastest) → openai-whisper → None
    """

    def __init__(self, model_size: str = "tiny"):
        """Initialise the instance."""
        self.model_size = model_size
        self._model     = None
        self._backend   = None
        self._ready     = False

    def init(self) -> bool:
        # Try faster-whisper first (much faster)
        """Initialise the operation.


            Returns:
                bool: Result.
            """
        try:
            from faster_whisper import WhisperModel
            self._model   = WhisperModel(self.model_size, device="cpu",
                                          compute_type="int8")
            self._backend = "faster-whisper"
            self._ready   = True
            return True
        except ImportError:
            pass
        # Fall back to openai-whisper
        try:
            import whisper
            self._model   = whisper.load_model(self.model_size)
            self._backend = "openai-whisper"
            self._ready   = True
            return True
        except ImportError:
            pass
        return False

    @property
    def available(self) -> bool:
        """Available.


            Returns:
                bool: Result.
            """
        if not self._ready:
            self.init()
        return self._ready

    def transcribe(self, audio_array) -> str:
        """Transcribe numpy audio array (float32, 16kHz mono)."""
        if not self.available:
            return ""
        try:
            if self._backend == "faster-whisper":
                segments, _ = self._model.transcribe(audio_array,
                                                       beam_size=3, language="en")
                return " ".join(s.text for s in segments).strip()
            else:
                import numpy as np
                result = self._model.transcribe(audio_array, fp16=False)
                return result.get("text", "").strip()
        except Exception as e:
            return ""

    def transcribe_file(self, path: str) -> str:
        """Transcribe an audio file."""
        if not self.available:
            return ""
        try:
            import numpy as np
            if self._backend == "faster-whisper":
                segments, _ = self._model.transcribe(path, language="en")
                return " ".join(s.text for s in segments).strip()
            else:
                result = self._model.transcribe(path, fp16=False)
                return result.get("text","").strip()
        except Exception:
            return ""


class VoiceInterface:
    """
    Main voice controller.
    Listens in background, detects wake word, transcribes, executes commands.
    """

    def __init__(self, kernel: "NovaKernel"):
        """Initialise the instance."""
        self.kernel    = kernel
        self.tts       = TTSEngine()
        self.stt       = STTEngine()
        self._running  = False
        self._thread   = None
        self._cmd_cb:  Optional[Callable] = None   # called with transcribed command
        self._status   = "idle"
        self._wake_active = False

    def set_command_callback(self, cb: Callable):
        """Set callback for when a command is recognized."""
        self._cmd_cb = cb

    def say(self, text: str):
        """Speak text aloud."""
        self.tts.say(text)

    def start(self) -> bool:
        """Start the operation.


            Returns:
                bool: Result.
            """
        if self._running: return True
        if not self._check_deps(): return False

        self._running = True
        self._thread  = threading.Thread(target=self._listen_loop,
                                          daemon=True, name="nova-voice")
        self._thread.start()
        self._status = "listening"
        self.say("Nova voice interface ready. Say 'Nova' followed by your command.")
        return True

    def stop(self):
        """Stop the operation."""
        self._running = False
        self._status  = "idle"

    def _check_deps(self) -> bool:
        """Check deps and return the result.


            Returns:
                bool: Result.
            """
        missing = []
        try: import sounddevice
        except ImportError: missing.append("sounddevice")
        try: import numpy
        except ImportError: missing.append("numpy")
        if not self.stt.init():
            missing.append("faster-whisper  OR  openai-whisper")
        if missing:
            print(f"  Voice requires: pip install {' '.join(missing)}")
            return False
        return True

    def _listen_loop(self):
        """Listen for loop."""
        try:
            import sounddevice as sd
            import numpy as np
        except ImportError:
            self._running = False
            return

        while self._running:
            try:
                # Record a chunk
                audio = sd.rec(int(CHUNK_DURATION * SAMPLE_RATE),
                               samplerate=SAMPLE_RATE, channels=1,
                               dtype="float32")
                sd.wait()
                audio_1d = audio.flatten()

                # Check silence threshold
                rms = float(np.sqrt(np.mean(audio_1d**2)))
                if rms < 0.005:
                    continue   # silence — skip

                # Transcribe
                text = self.stt.transcribe(audio_1d).lower().strip()
                if not text:
                    continue

                # Wake word detection
                has_wake = any(w in text for w in WAKE_WORDS)
                if has_wake or self._wake_active:
                    # Strip wake word from command
                    command = text
                    for w in WAKE_WORDS:
                        command = command.replace(w, "").strip()

                    if command:
                        print(f"\r  [Voice] Heard: {command}")
                        if self._cmd_cb:
                            try:
                                self._cmd_cb(command)
                            except Exception: pass
                        self._wake_active = False
                    else:
                        self._wake_active = True   # wake word heard, waiting for command

            except Exception as e:
                time.sleep(0.5)

    def test(self) -> dict:
        """Test TTS and STT availability."""
        result = {
            "tts_available":  self.tts.available,
            "stt_available":  self.stt.available,
            "stt_backend":    self.stt._backend,
            "tts_voices":     self.tts.voices()[:3],
        }
        if self.tts.available:
            self.tts.say("PyOS NOVA voice test successful.")
        return result

    @property
    def status(self) -> dict:
        """Return the current status as a dict.


            Returns:
                dict: Result.
            """
        return {
            "running":       self._running,
            "status":        self._status,
            "tts_available": self.tts.available,
            "stt_available": self.stt.available,
            "stt_backend":   self.stt._backend or "none",
            "wake_words":    WAKE_WORDS,
        }
