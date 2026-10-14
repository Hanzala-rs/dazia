"""Local speech-to-text with faster-whisper (runs on the CPU, nothing leaves the machine).

Flow: recording bytes -> decode in memory (PyAV, any browser format) -> length check -> Whisper -> text.
The transcript is private: only durations and timings are logged, never the text.
"""
import io
import logging
import threading
import time

from faster_whisper import WhisperModel, decode_audio

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000  # decode_audio's default; what Whisper expects
_UNREADABLE = "Couldn't read that recording — please try again."


class AudioError(ValueError):
    """The uploaded audio is empty, undecodable, or too long. Message is safe to show the user."""


class SpeechToText:
    def __init__(self, model_name: str, models_dir: str = "models/whisper", max_seconds: float = 30.0):
        """Cheap: the model is loaded lazily on first transcribe() and then kept (thread-safe)."""
        self.model_name = model_name
        self.models_dir = models_dir
        self.max_seconds = max_seconds
        self._model = None
        self._lock = threading.Lock()

    def _get_model(self):
        if self._model is None:
            with self._lock:
                if self._model is None:  # another thread may have loaded it while we waited
                    start = time.perf_counter()
                    opts = dict(device="cpu", compute_type="int8", download_root=self.models_dir)
                    try:
                        # Local-first: use the cached model without contacting the internet.
                        self._model = WhisperModel(self.model_name, local_files_only=True, **opts)
                    except Exception:
                        # Not cached yet: download it once (the only time voice input needs the internet).
                        log.info("Whisper model %s not cached; downloading once", self.model_name)
                        self._model = WhisperModel(self.model_name, **opts)
                    log.info("Loaded Whisper model %s in %.2fs", self.model_name, time.perf_counter() - start)
        return self._model

    def transcribe(self, audio: bytes) -> str:
        """Decode any browser recording format (webm/opus, ogg, wav, mp4) and return the English text."""
        if not audio:
            raise AudioError(_UNREADABLE)
        try:
            samples = decode_audio(io.BytesIO(audio), sampling_rate=SAMPLE_RATE)
        except Exception as exc:  # PyAV raises many error types for bad/unknown containers
            log.info("Audio decode failed (%d bytes): %s", len(audio), type(exc).__name__)
            raise AudioError(_UNREADABLE) from exc
        duration = len(samples) / SAMPLE_RATE
        if duration <= 0:
            raise AudioError(_UNREADABLE)
        if duration > self.max_seconds:
            raise AudioError(f"Recording is too long (max {self.max_seconds:g} seconds).")

        model = self._get_model()
        start = time.perf_counter()
        segments, _info = model.transcribe(
            samples, language="en", beam_size=1, vad_filter=True, condition_on_previous_text=False
        )
        # segments is a lazy generator: the actual inference happens while joining.
        text = " ".join(t for t in (seg.text.strip() for seg in segments) if t)
        log.info("Transcribed %.1fs of audio in %.2fs", duration, time.perf_counter() - start)
        return text
