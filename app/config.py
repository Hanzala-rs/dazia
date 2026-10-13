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


settings = Settings()
if not settings.timezone:
    from tzlocal import get_localzone_name

    settings.timezone = get_localzone_name()
