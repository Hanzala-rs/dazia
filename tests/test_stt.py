"""Speech-to-text tests.

Unit tests replace WhisperModel/decode_audio with fakes. One integration test runs the real cached
base.en model on speech synthesized by Piper (skipped if either model is missing). Route tests use the
same fakes pattern as test_api.py. No network is used.
"""
import io
import threading
import time
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.services import speech_to_text as stt_mod
from app.services.speech_to_text import AudioError, SpeechToText

ROOT = Path(__file__).resolve().parent.parent
WHISPER_DIR = ROOT / "models" / "whisper"
PIPER_VOICE = ROOT / "models" / "piper" / "en_US-lessac-medium.onnx"


# ---- fakes ---------------------------------------------------------------------------

class FakeWhisper:
    loads = 0
    segments = [" Hello", " there. ", ""]

    def __init__(self, model_name, device, compute_type, download_root):
        assert (device, compute_type) == ("cpu", "int8")
        time.sleep(0.05)  # widen the race window for the concurrency test
        FakeWhisper.loads += 1
        self.calls = []

    def transcribe(self, audio, **kwargs):
        self.calls.append(kwargs)
        return iter(SimpleNamespace(text=t) for t in self.segments), SimpleNamespace()


@pytest.fixture
def fake_whisper(monkeypatch):
    FakeWhisper.loads = 0
    monkeypatch.setattr(stt_mod, "WhisperModel", FakeWhisper)
    monkeypatch.setattr(stt_mod, "decode_audio", lambda f, sampling_rate: np.zeros(sampling_rate * 2, np.float32))
    return FakeWhisper


# ---- unit tests ----------------------------------------------------------------------

def test_constructor_is_cheap(fake_whisper):
    SpeechToText("base.en")
    assert fake_whisper.loads == 0


def test_model_loaded_once_and_options(fake_whisper):
    stt = SpeechToText("base.en")
    assert stt.transcribe(b"x") == "Hello there."
    assert stt.transcribe(b"x") == "Hello there."
    assert fake_whisper.loads == 1
    assert stt._model.calls[0] == {
        "language": "en", "beam_size": 1, "vad_filter": True, "condition_on_previous_text": False,
    }


def test_model_loaded_once_under_concurrency(fake_whisper):
    stt = SpeechToText("base.en")
    barrier = threading.Barrier(2)
    results = []

    def worker():
        barrier.wait()
        results.append(stt.transcribe(b"x"))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == ["Hello there.", "Hello there."]
    assert fake_whisper.loads == 1


def test_empty_bytes_rejected(fake_whisper):
    with pytest.raises(AudioError, match="Couldn't read"):
        SpeechToText("base.en").transcribe(b"")
    assert fake_whisper.loads == 0


def test_zero_length_audio_rejected(fake_whisper, monkeypatch):
    monkeypatch.setattr(stt_mod, "decode_audio", lambda f, sampling_rate: np.zeros(0, np.float32))
    with pytest.raises(AudioError, match="Couldn't read"):
        SpeechToText("base.en").transcribe(b"x")


def test_undecodable_rejected(fake_whisper, monkeypatch):
    def boom(f, sampling_rate):
        raise RuntimeError("Invalid data found when processing input")

    monkeypatch.setattr(stt_mod, "decode_audio", boom)
    with pytest.raises(AudioError, match="Couldn't read"):
        SpeechToText("base.en").transcribe(b"x")


def test_garbage_bytes_rejected_by_real_decoder(fake_whisper, monkeypatch):
    monkeypatch.setattr(stt_mod, "decode_audio", __import__("faster_whisper").decode_audio)
    with pytest.raises(AudioError, match="Couldn't read"):
        SpeechToText("base.en").transcribe(b"definitely not audio" * 50)


def test_too_long_rejected(fake_whisper):
    stt = SpeechToText("base.en", max_seconds=1.5)  # fake decode returns 2s
    with pytest.raises(AudioError, match=r"too long \(max 1.5 seconds\)"):
        stt.transcribe(b"x")
    assert fake_whisper.loads == 0


def test_silence_returns_empty(fake_whisper, monkeypatch):
    monkeypatch.setattr(FakeWhisper, "segments", [])
    assert SpeechToText("base.en").transcribe(b"x") == ""


# ---- real model integration ------------------------------------------------------------

def _synth_wav(text: str) -> bytes:
    if not PIPER_VOICE.is_file() or not PIPER_VOICE.with_suffix(".onnx.json").is_file():
        pytest.skip("Piper voice model not downloaded")
    if not any(WHISPER_DIR.glob("models--Systran--faster-whisper-base.en")):
        pytest.skip("Whisper base.en model not cached")
    piper = pytest.importorskip("piper")
    voice = piper.PiperVoice.load(str(PIPER_VOICE))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav_file:
        voice.synthesize_wav(text, wav_file)
    return buf.getvalue()


def _wav_to_webm_opus(wav_bytes: bytes) -> bytes:
    av = pytest.importorskip("av")
    if "libopus" not in av.codecs_available:
        pytest.skip("libopus encoder not available in this PyAV build")
    src = av.open(io.BytesIO(wav_bytes))
    out_buf = io.BytesIO()
    dst = av.open(out_buf, "w", format="webm")
    stream = dst.add_stream("libopus", rate=48000)
    stream.layout = "mono"
    resampler = av.AudioResampler(format="s16", layout="mono", rate=48000)
    for frame in src.decode(audio=0):
        for rf in resampler.resample(frame):
            for packet in stream.encode(rf):
                dst.mux(packet)
    for rf in resampler.resample(None):
        for packet in stream.encode(rf):
            dst.mux(packet)
    for packet in stream.encode(None):
        dst.mux(packet)
    dst.close()
    src.close()
    return out_buf.getvalue()


@pytest.fixture(scope="module")
def spoken_wav():
    return _synth_wav("Remind me to call the bank tomorrow at nine.")


@pytest.fixture(scope="module")
def real_stt():
    return SpeechToText("base.en", models_dir=str(WHISPER_DIR))


def test_real_model_transcribes_wav(spoken_wav, real_stt):
    text = real_stt.transcribe(spoken_wav)
    assert "tomorrow" in text.lower()


def test_real_model_transcribes_webm_opus(spoken_wav, real_stt):
    webm = _wav_to_webm_opus(spoken_wav)
    assert webm[:4] == b"\x1a\x45\xdf\xa3"  # EBML magic: a real WebM container
    text = real_stt.transcribe(webm)
    assert "tomorrow" in text.lower()


# ---- route tests ---------------------------------------------------------------------

class FakeLLM:
    def __init__(self, *args, **kwargs):
        pass


class FakeAssistant:
    def __init__(self, *args, **kwargs):
        pass


class FakeScheduler:
    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        pass

    def shutdown(self):
        pass


class FakeTTS:
    def __init__(self, *args, **kwargs):
        pass


class FakeSTT:
    def __init__(self, result="hello world", error=None):
        self.result, self.error, self.received = result, error, []

    def transcribe(self, audio):
        self.received.append(audio)
        if self.error:
            raise self.error
        return self.result


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "OllamaClient", FakeLLM)
    monkeypatch.setattr(main, "Assistant", FakeAssistant)
    monkeypatch.setattr(main, "ReminderScheduler", FakeScheduler)
    monkeypatch.setattr(main, "TextToSpeech", FakeTTS)
    monkeypatch.setattr(main, "SpeechToText", lambda *a, **k: FakeSTT())
    monkeypatch.setattr(main.settings, "passphrase_hash", "")
    monkeypatch.setattr(main.settings, "host", "127.0.0.1")
    with TestClient(main.app) as c:
        c.app.state.stt = FakeSTT()
        yield c


def _post(client, body=b"RIFFfake", content_type="audio/webm;codecs=opus"):
    return client.post("/transcribe", content=body, headers={"Content-Type": content_type})


def test_transcribe_ok(client):
    res = _post(client)
    assert res.status_code == 200
    assert res.json() == {"text": "hello world"}
    assert client.app.state.stt.received == [b"RIFFfake"]


def test_transcribe_wrong_content_type(client):
    assert _post(client, content_type="application/json").status_code == 415
    assert client.app.state.stt.received == []


def test_transcribe_too_large(client, monkeypatch):
    monkeypatch.setattr(main.settings, "max_audio_bytes", 10)
    assert _post(client, body=b"x" * 11).status_code == 413
    assert client.app.state.stt.received == []


def test_transcribe_audio_error(client):
    client.app.state.stt = FakeSTT(error=AudioError("Couldn't read that recording — please try again."))
    res = _post(client)
    assert res.status_code == 400
    assert res.json()["detail"] == "Couldn't read that recording — please try again."


def test_transcribe_disabled(client):
    client.app.state.stt = None
    assert _post(client).status_code == 404
