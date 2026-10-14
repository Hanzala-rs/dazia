"""DAZIA configuration. Values come from environment variables / .env (prefix DAZIA_). No secrets in code."""
from datetime import time

from pydantic import field_validator
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
    # Phase 6 Gmail (read-only). Off by default: the default path calls no external API.
    gmail_enabled: bool = False
    gmail_client_file: str = "secrets/gmail_client.json"  # OAuth client from Google Cloud Console
    gmail_token_file: str = "secrets/gmail_token.bin"  # DPAPI-encrypted; created by: python -m app.services.gmail
    gmail_timeout_s: float = 15.0
    # Phase 11 Outlook.com (read-only, Microsoft Graph Mail.Read). Off by default.
    outlook_enabled: bool = False
    outlook_client_id: str = ""  # Application (client) ID of your app registration; not a secret
    outlook_token_file: str = "secrets/outlook_token.bin"  # DPAPI-encrypted; created by: python -m app.services.outlook
    outlook_timeout_s: float = 15.0
    # Phase 7 daily briefing: local time "HH:MM" (24-hour) to send it each day. Empty = no scheduled briefing.
    briefing_time: str = ""

    @field_validator("briefing_time")
    @classmethod
    def check_briefing_time(cls, value: str) -> str:
        value = value.strip()
        if value:
            time.fromisoformat(value)  # raises ValueError -> clear startup error for a bad value
        return value


settings = Settings()
if not settings.timezone:
    from tzlocal import get_localzone_name

    settings.timezone = get_localzone_name()
