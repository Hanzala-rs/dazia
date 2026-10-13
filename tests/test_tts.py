"""Text-to-speech tests. Unit tests use a fake PiperVoice (no model, no network);
one test uses the real downloaded voice and is skipped if it isn't there."""
import io
import threading
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.services import text_to_speech as tts_mod
from app.services.text_to_speech import TextToSpeech, TTSUnavailable, _speakable, _truncate
from tests.test_api import FakeAssistant, FakeLLM, FakeScheduler

REAL_VOICE = Path(__file__).resolve().parent.parent / "models" / "piper" / "en_US-lessac-medium.onnx"


class FakeConfig:
    sample_rate = 16000


class FakeVoice:
    loads = 0
    spoken = []

    def __init__(self):
        self.config = FakeConfig()

    @classmethod
    def load(cls, path):
        cls.loads += 1
        return cls()

    def synthesize_wav(self, text, wav_file):
        FakeVoice.spoken.append(text)
        wav_file.writeframes(b"\x01\x00" * 160)


@pytest.fixture
def voice_files(tmp_path, monkeypatch):
    """Empty placeholder voice files + a fake loader."""
    model = tmp_path / "voice.onnx"
    model.write_bytes(b"")
    (tmp_path / "voice.onnx.json").write_text("{}", encoding="utf-8")
    FakeVoice.loads = 0
    FakeVoice.spoken = []
    monkeypatch.setattr(tts_mod, "_load_voice", FakeVoice.load)
    return str(model)


def _read_wav(data: bytes):
    with wave.open(io.BytesIO(data), "rb") as w:
        return w.getframerate(), w.getnframes()


# ---- loading ------------------------------------------------------------------------------

def test_constructor_is_cheap(monkeypatch):
    monkeypatch.setattr(tts_mod, "_load_voice", lambda p: pytest.fail("loaded in constructor"))
    TextToSpeech("does/not/exist.onnx")


def test_voice_loaded_once(voice_files):
    tts = TextToSpeech(voice_files)
    for _ in range(3):
        rate, frames = _read_wav(tts.synthesize("Hello there."))
        assert (rate, frames) == (16000, 160)
    assert FakeVoice.loads == 1


def test_voice_loaded_once_across_threads(voice_files):
    tts = TextToSpeech(voice_files)
    threads = [threading.Thread(target=tts.synthesize, args=("Hi.",)) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert FakeVoice.loads == 1
    assert len(FakeVoice.spoken) == 5


def test_missing_model_gives_download_hint(tmp_path):
    with pytest.raises(TTSUnavailable) as exc:
        TextToSpeech(str(tmp_path / "missing.onnx")).synthesize("Hi")
    assert "piper.download_voices en_US-lessac-medium --download-dir models/piper" in str(exc.value)


def test_missing_config_gives_download_hint(voice_files):
    Path(voice_files + ".json").unlink()
    with pytest.raises(TTSUnavailable) as exc:
        TextToSpeech(voice_files).synthesize("Hi")
    assert ".onnx.json" in str(exc.value)
    assert "piper.download_voices" in str(exc.value)


def test_load_failure_gives_download_hint(voice_files, monkeypatch):
    def broken(path):
        raise RuntimeError("corrupt model")

    monkeypatch.setattr(tts_mod, "_load_voice", broken)
    with pytest.raises(TTSUnavailable) as exc:
        TextToSpeech(voice_files).synthesize("Hi")
    assert "corrupt model" in str(exc.value)
    assert "piper.download_voices" in str(exc.value)


def test_text_is_not_logged(voice_files, caplog):
    caplog.set_level("DEBUG")
    TextToSpeech(voice_files).synthesize("my secret plans")
    assert "secret" not in caplog.text
    assert "chars" in caplog.text


def test_nothing_speakable_gives_valid_silent_wav(voice_files):
    rate, frames = _read_wav(TextToSpeech(voice_files).synthesize("***"))
    assert (rate, frames) == (16000, 0)
    assert FakeVoice.spoken == []


def test_synthesize_truncates(voice_files):
    TextToSpeech(voice_files, max_chars=50).synthesize("word " * 100)
    assert len(FakeVoice.spoken[0]) <= 50


# ---- text preparation ---------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("Hello world", "Hello world."),
    ("# Title\nSome **bold** text", "Title. Some bold text."),
    ("Reminder #12 is set", "Reminder number 12 is set."),
    ("Tea—then work", "Tea, then work."),
    ("- milk\n- eggs\n\n1. bread", "milk. eggs. bread."),
    ("Done!\nNext?", "Done! Next?"),
    ("See https://example.com/x?y=1 now", "See a link now."),
    ("`code` and _under_", "code and under."),
    ("", ""),
    ("  \n * \n", ""),
])
def test_speakable(raw, expected):
    assert _speakable(raw) == expected


def test_truncate_short_text_unchanged():
    assert _truncate("Hi there.", 100) == "Hi there."


def test_truncate_at_sentence_boundary():
    text = "First sentence here. Second sentence is much longer than the limit."
    assert _truncate(text, 34) == "First sentence here."


def test_truncate_at_word_boundary():
    text = "one two three four five six seven"
    out = _truncate(text, 16)
    assert out == "one two three."
    assert len(out) <= 16


def test_truncate_single_long_word():
    assert _truncate("x" * 50, 10) == "x" * 10


# ---- real voice ---------------------------------------------------------------------------

@pytest.mark.skipif(not REAL_VOICE.is_file(), reason="Piper voice not downloaded")
def test_real_voice_produces_wav():
    import json

    expected_rate = json.loads(Path(str(REAL_VOICE) + ".json").read_text(encoding="utf-8"))["audio"]["sample_rate"]
    data = TextToSpeech(str(REAL_VOICE)).synthesize("Hello, this is DAZIA.")
    assert data[:4] == b"RIFF" and data[8:12] == b"WAVE"
    rate, frames = _read_wav(data)
    assert rate == expected_rate
    assert frames > 0


# ---- POST /speak ----------------------------------------------------------------------------

class FakeSTT:
    def __init__(self, *args, **kwargs):
        pass


class FakeTTS:
    def __init__(self, error=None):
        self.error = error
        self.texts = []

    def synthesize(self, text):
        if self.error:
            raise self.error
        self.texts.append(text)
        return b"RIFF-fake-wav"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "OllamaClient", FakeLLM)
    monkeypatch.setattr(main, "Assistant", FakeAssistant)
    monkeypatch.setattr(main, "ReminderScheduler", FakeScheduler)
    monkeypatch.setattr(main, "SpeechToText", FakeSTT)
    monkeypatch.setattr(main.settings, "passphrase_hash", "")
    monkeypatch.setattr(main.settings, "host", "127.0.0.1")
    monkeypatch.setattr(main.settings, "tts_enabled", True)
    with TestClient(main.app) as c:
        c.app.state.tts = FakeTTS()
        yield c


def test_speak_returns_wav(client):
    res = client.post("/speak", json={"text": "  Hello  "})
    assert res.status_code == 200
    assert res.headers["content-type"] == "audio/wav"
    assert res.headers["cache-control"] == "no-store"
    assert res.content == b"RIFF-fake-wav"
    assert client.app.state.tts.texts == ["Hello"]


@pytest.mark.parametrize("text", ["", "   \n "])
def test_speak_empty_text_rejected(client, text):
    assert client.post("/speak", json={"text": text}).status_code == 422
    assert client.app.state.tts.texts == []


def test_speak_voice_unavailable(client):
    client.app.state.tts = FakeTTS(error=TTSUnavailable("Voice file not found. Download it."))
    res = client.post("/speak", json={"text": "Hello"})
    assert res.status_code == 503
    assert "Download" in res.json()["detail"]


def test_speak_disabled(client):
    client.app.state.tts = None
    assert client.post("/speak", json={"text": "Hello"}).status_code == 404


def test_voice_status_reports_tts(client):
    assert client.get("/voice/status").json()["tts_enabled"] is True
