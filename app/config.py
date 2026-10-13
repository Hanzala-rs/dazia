"""DAZIA configuration. Values come from environment variables / .env (prefix DAZIA_). No secrets in code."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DAZIA_", env_file=".env", extra="ignore")

    db_path: str = "dazia.db"
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen3.5:4b"
    ollama_timeout_s: float = 120.0
    timezone: str = ""  # IANA name, e.g. "Asia/Karachi"; empty = use the system time zone
    host: str = "127.0.0.1"
    port: int = 8000
    max_message_chars: int = 2000
    # Phase 3 login. Empty = no login (only allowed while host is loopback). Generate with: python -m app.auth
    passphrase_hash: str = ""
    session_days: int = 30
    # Phase 4 voice (local only). Each can be switched off independently.
    stt_enabled: bool = True
    stt_model: str = "base.en"  # faster-whisper model; downloaded once into models/whisper
    tts_enabled: bool = True
    tts_voice: str = "models/piper/en_US-lessac-medium.onnx"  # see README for the download command
    max_audio_bytes: int = 5_000_000
    max_audio_seconds: float = 30.0
    max_tts_chars: int = 1000


settings = Settings()
if not settings.timezone:
    from tzlocal import get_localzone_name

    settings.timezone = get_localzone_name()
