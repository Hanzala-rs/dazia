"""DAZIA web API.

Request flow:  browser -> POST /chat -> Assistant.handle() -> (LLM + tools) -> JSON reply.
Run locally:   .venv/Scripts/python -m app.main   (binds 127.0.0.1 by default)
Login (Phase 3): when DAZIA_PASSPHRASE_HASH is set, every data route needs a session cookie (see app/auth.py).
"""
import asyncio
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

from app import auth, db
from app.assistant import Assistant, AssistantReply
from app.config import settings
from app.llm import OllamaClient
from app.scheduler import ReminderScheduler
from app.services.notify import Notifier
from app.services.speech_to_text import AudioError, SpeechToText
from app.services.text_to_speech import TextToSpeech, TTSUnavailable

STATIC_DIR = Path(__file__).parent / "static"

# One local model on CPU: only one chat request runs at a time.
_chat_lock = threading.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Runs under `python -m app.main` and plain uvicorn alike: never serve the LAN without a login.
    auth.check_startup_safety(settings.host, settings.passphrase_hash)
    # Expensive resources are created once and shared by every request.
    app.state.llm = OllamaClient(settings.ollama_url, settings.ollama_model, settings.ollama_timeout_s)
    app.state.assistant = Assistant(app.state.llm, settings.db_path, settings.timezone)
    # Voice models load lazily on first use, so startup stays fast and RAM is only used if voice is used.
    app.state.stt = SpeechToText(settings.stt_model, max_seconds=settings.max_audio_seconds) if settings.stt_enabled else None
    app.state.tts = TextToSpeech(settings.tts_voice, max_chars=settings.max_tts_chars) if settings.tts_enabled else None
    # Reminder delivery: SQLite is the source of truth; start() first delivers anything missed while stopped.
    app.state.scheduler = ReminderScheduler(settings.db_path, notify=Notifier(settings.db_path, settings.timezone))
    await asyncio.to_thread(app.state.scheduler.start)
    yield
    app.state.scheduler.shutdown()


# The interactive API docs are only exposed when no login is configured (local-only use).
_docs_open = not settings.passphrase_hash
app = FastAPI(
    title="DAZIA",
    lifespan=lifespan,
    docs_url="/docs" if _docs_open else None,
    redoc_url="/redoc" if _docs_open else None,
    openapi_url="/openapi.json" if _docs_open else None,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def block_remote_without_login(request: Request, call_next):
    """Remote access (e.g. through `tailscale serve`) is refused until a login passphrase is set."""
    if not auth.auth_required() and auth.is_proxied(request):
        return JSONResponse(
            status_code=403,
            content={"detail": "Remote access needs a login. Set DAZIA_PASSPHRASE_HASH in .env (see README)."},
        )
    return await call_next(request)


class ChatRequest(BaseModel):
    message: str

    @field_validator("message")
    @classmethod
    def check_message(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("message must not be empty")
        if len(value) > settings.max_message_chars:
            raise ValueError(f"message must be at most {settings.max_message_chars} characters")
        return value


class ReminderOut(BaseModel):
    id: int
    title: str
    due_utc: str  # ISO-8601 UTC
    tz: str
    status: str


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


def _root_static(name: str, media_type: str) -> FileResponse:
    # Served at "/" (not /static) so the service worker's scope can be the whole app.
    path = STATIC_DIR / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Not Found")
    return FileResponse(path, media_type=media_type)


@app.get("/manifest.json")
def manifest() -> FileResponse:
    return _root_static("manifest.json", "application/manifest+json")


@app.get("/sw.js")
def service_worker() -> FileResponse:
    return _root_static("sw.js", "application/javascript")


# ---- login (public routes) -----------------------------------------------------------

class LoginRequest(BaseModel):
    passphrase: str


@app.get("/auth/status")
def auth_status(request: Request) -> dict:
    return {"auth_required": auth.auth_required(), "authenticated": auth.is_authenticated(request)}


@app.post("/login")
def login(body: LoginRequest, request: Request, response: Response) -> dict:
    if not auth.auth_required():
        return {"ok": True}
    ip = request.client.host if request.client else "unknown"
    if auth.too_many_failures(ip):
        raise HTTPException(status_code=429, detail="Too many wrong passphrases. Wait a few minutes and try again.")
    if not auth.verify_passphrase(body.passphrase, settings.passphrase_hash):
        auth.record_failure(ip)
        raise HTTPException(status_code=401, detail="Wrong passphrase")
    auth.reset_failures(ip)
    auth.set_session_cookie(response, request, auth.create_session())
    return {"ok": True}


@app.post("/logout")
def logout(request: Request, response: Response) -> dict:
    auth.end_session(request.cookies.get(auth.COOKIE_NAME))
    auth.clear_session_cookie(response, request)
    return {"ok": True}


# ---- data routes (login required when a passphrase is set) ------------------------------
_protected = [Depends(auth.require_auth)]


@app.get("/health", dependencies=_protected)
def health(request: Request) -> dict:
    ok, detail = request.app.state.llm.health()
    return {"status": "ok", "model": settings.ollama_model, "llm_ok": bool(ok), "llm_detail": str(detail)}


def _handle_locked(assistant, message: str) -> AssistantReply:
    with _chat_lock:
        return assistant.handle(message)


@app.post("/chat", response_model=AssistantReply, dependencies=_protected)
async def chat(body: ChatRequest, request: Request) -> AssistantReply:
    # handle() is blocking (model inference), so run it off the event loop.
    return await run_in_threadpool(_handle_locked, request.app.state.assistant, body.message)


# ---- Voice (Phase 4) ------------------------------------------------------------
# Speech-to-text shares the chat lock: on this CPU, STT and the LLM must not run at the same time.


@app.get("/voice/status", dependencies=_protected)
def voice_status(request: Request) -> dict:
    return {"stt_enabled": request.app.state.stt is not None, "tts_enabled": request.app.state.tts is not None}


def _transcribe_locked(stt: SpeechToText, audio: bytes) -> str:
    with _chat_lock:
        return stt.transcribe(audio)


@app.post("/transcribe", dependencies=_protected)
async def transcribe(request: Request) -> dict:
    """Body = the raw recording (Content-Type audio/*). Returns {"text": ...} for the user to check before sending."""
    stt = request.app.state.stt
    if stt is None:
        raise HTTPException(status_code=404, detail="Voice input is switched off")
    if not request.headers.get("content-type", "").startswith("audio/"):
        raise HTTPException(status_code=415, detail="Send the recording with an audio/* Content-Type")
    audio = bytearray()
    async for chunk in request.stream():  # read with a size cap instead of trusting Content-Length
        audio.extend(chunk)
        if len(audio) > settings.max_audio_bytes:
            raise HTTPException(status_code=413, detail="Recording is too large")
    try:
        text = await run_in_threadpool(_transcribe_locked, stt, bytes(audio))
    except AudioError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"text": text}


class SpeakRequest(BaseModel):
    text: str

    @field_validator("text")
    @classmethod
    def check_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("text must not be empty")
        return value[: settings.max_tts_chars]


@app.post("/speak", dependencies=_protected)
async def speak(body: SpeakRequest, request: Request) -> Response:
    tts = request.app.state.tts
    if tts is None:
        raise HTTPException(status_code=404, detail="Voice output is switched off")
    try:
        wav = await run_in_threadpool(tts.synthesize, body.text)
    except TTSUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return Response(content=wav, media_type="audio/wav", headers={"Cache-Control": "no-store"})


@app.get("/reminders", response_model=list[ReminderOut], dependencies=_protected)
def reminders(status: str = Query(db.STATUS_PENDING, pattern=f"^({db.STATUS_PENDING}|{db.STATUS_DONE})$")):
    conn = db.connect(settings.db_path)
    try:
        items = db.list_reminders(conn, status=status)
    finally:
        conn.close()
    return [
        ReminderOut(id=r.id, title=r.title, due_utc=r.due_utc.isoformat(), tz=r.tz, status=r.status)
        for r in items
    ]


class NotificationOut(BaseModel):
    id: int
    reminder_id: int | None
    title: str
    body: str
    kind: str  # "reminder" | "missed"
    created_at: str  # ISO-8601 UTC
    read_at: str | None  # ISO-8601 UTC, null while unread


@app.get("/notifications", response_model=list[NotificationOut], dependencies=_protected)
def notifications(unread_only: bool = True, limit: int = Query(20, ge=1, le=100)):
    """Feed of delivered reminders, newest first."""
    conn = db.connect(settings.db_path)
    try:
        items = db.list_notifications(conn, unread_only=unread_only, limit=limit)
    finally:
        conn.close()
    return [
        NotificationOut(
            id=n.id, reminder_id=n.reminder_id, title=n.title, body=n.body, kind=n.kind,
            created_at=n.created_at.isoformat(),
            read_at=n.read_at.isoformat() if n.read_at else None,
        )
        for n in items
    ]


@app.post("/notifications/{notification_id}/read", dependencies=_protected)
def notification_read(notification_id: int) -> dict:
    """Mark one notification read. Always 200: {"ok": false} means it was unknown or already read,
    so dismissing twice (e.g. from two browser tabs) is harmless."""
    conn = db.connect(settings.db_path)
    try:
        ok = db.mark_notification_read(conn, notification_id)
    finally:
        conn.close()
    return {"ok": bool(ok)}


if __name__ == "__main__":
    # proxy_headers: trust X-Forwarded-Proto/-For only from 127.0.0.1 (i.e. `tailscale serve` on this laptop),
    # so cookies get the Secure flag over HTTPS and login rate limiting sees the real client.
    uvicorn.run(
        "app.main:app", host=settings.host, port=settings.port, proxy_headers=True, forwarded_allow_ips="127.0.0.1"
    )
