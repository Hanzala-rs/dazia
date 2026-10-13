"""Local text-to-speech with Piper. Nothing leaves the laptop.

Flow: synthesize(text) -> _speakable() cleans the text -> _truncate() caps its length
-> Piper writes a WAV into memory -> bytes go back to POST /speak.
"""
import io
import logging
import re
import threading
import time
import wave
from pathlib import Path

log = logging.getLogger(__name__)

DOWNLOAD_HINT = (
    "Download the voice with: "
    r".venv\Scripts\python -m piper.download_voices en_US-lessac-medium --download-dir models/piper"
)


class TTSUnavailable(RuntimeError):
    """The voice model file is missing or failed to load. Message tells the user how to fix it."""


# ---- text preparation -----------------------------------------------------------------

_URL = re.compile(r"https?://\S+")
_HASH_ID = re.compile(r"#(\d+)")                 # "#12" -> "number 12"
_DASH = re.compile(r"\s*[—–]\s*")      # em/en dash -> pause
_LIST_MARK = re.compile(r"^\s*(?:[-+>]|\d+[.)])\s+")  # "- item", "> quote", "1. item" at line start
_SYMBOLS = re.compile(r"[#*_`~|<>\[\]{}]")        # markdown-ish symbols read out as noise
_SPACES = re.compile(r"[ \t]+")


def _speakable(text: str) -> str:
    """Turn chat text into something that sounds natural when read aloud."""
    text = _URL.sub("a link", text)          # reading a URL letter by letter is useless
    text = _HASH_ID.sub(r"number \1", text)  # before stripping '#'
    text = _DASH.sub(", ", text)
    sentences = []
    for line in text.splitlines():           # each line becomes its own sentence
        line = _LIST_MARK.sub("", line)
        line = _SYMBOLS.sub("", line)
        line = _SPACES.sub(" ", line).strip(" ,")
        if not line:
            continue
        if line[-1] not in ".!?:;,":
            line += "."                      # so Piper pauses between lines
        sentences.append(line)
    return " ".join(sentences)


def _truncate(text: str, max_chars: int) -> str:
    """Cap length, cutting at the last sentence end (or word) so speech doesn't stop mid-word."""
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    sentence_end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if sentence_end >= max_chars // 2:
        return cut[: sentence_end + 1]
    space = cut.rfind(" ")
    if space > 0:
        return cut[:space].rstrip(" ,") + "."
    return cut


# ---- synthesis ----------------------------------------------------------------------------

def _load_voice(voice_path: str):
    from piper import PiperVoice  # imported lazily: onnxruntime is heavy and only needed if voice is used

    return PiperVoice.load(voice_path)


class TextToSpeech:
    def __init__(self, voice_path: str, max_chars: int = 1000):
        """Cheap: the voice is loaded lazily on first synthesize() and then kept (thread-safe)."""
        self.voice_path = voice_path
        self.max_chars = max_chars
        self._voice = None
        self._load_lock = threading.Lock()
        # One synthesis at a time: sharing one onnxruntime session across threads isn't relied on.
        self._synth_lock = threading.Lock()

    def _get_voice(self):
        if self._voice is None:
            with self._load_lock:
                if self._voice is None:  # double-checked: another thread may have loaded it meanwhile
                    self._voice = self._load()
        return self._voice

    def _load(self):
        model = Path(self.voice_path)
        config = Path(self.voice_path + ".json")
        for path in (model, config):
            if not path.is_file():
                raise TTSUnavailable(f"Voice file not found: {path}. {DOWNLOAD_HINT}")
        start = time.perf_counter()
        try:
            voice = _load_voice(str(model))
        except Exception as exc:
            log.warning("Piper voice failed to load: %s", exc)
            raise TTSUnavailable(f"Voice model could not be loaded ({exc}). {DOWNLOAD_HINT}") from exc
        log.info("Piper voice loaded in %.1fs", time.perf_counter() - start)
        return voice

    def synthesize(self, text: str) -> bytes:
        """Return a complete WAV file (bytes) speaking `text` (truncated to max_chars)."""
        voice = self._get_voice()
        prepared = _truncate(_speakable(text), self.max_chars)
        buf = io.BytesIO()
        start = time.perf_counter()
        with self._synth_lock:
            with wave.open(buf, "wb") as wav:
                # Set the format up front so text with nothing speakable still yields a valid (silent) WAV.
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(voice.config.sample_rate)
                if prepared:
                    voice.synthesize_wav(prepared, wav)
        # Never log the text itself (it may be private); size and timing only.
        log.info("TTS: %d chars in %.1fs", len(prepared), time.perf_counter() - start)
        return buf.getvalue()
