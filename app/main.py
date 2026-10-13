"""DAZIA web API.

Request flow:  browser -> POST /chat -> Assistant.handle() -> (LLM + tools) -> JSON reply.
Run locally:   .venv/Scripts/python -m app.main   (binds 127.0.0.1 by default)
"""
import asyncio
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

from app import db
from app.assistant import Assistant, AssistantReply
from app.config import settings
from app.llm import OllamaClient
from app.scheduler import ReminderScheduler
from app.services.notify import Notifier

STATIC_DIR = Path(__file__).parent / "static"

# One local model on CPU: only one chat request runs at a time.
_chat_lock = threading.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Expensive resources are created once and shared by every request.
    app.state.llm = OllamaClient(settings.ollama_url, settings.ollama_model, settings.ollama_timeout_s)
    app.state.assistant = Assistant(app.state.llm, settings.db_path, settings.timezone)
    # Reminder delivery: SQLite is the source of truth; start() first delivers anything missed while stopped.
    app.state.scheduler = ReminderScheduler(settings.db_path, notify=Notifier(settings.db_path, settings.timezone))
    await asyncio.to_thread(app.state.scheduler.start)
    yield
    app.state.scheduler.shutdown()


app = FastAPI(title="DAZIA", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


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


@app.get("/health")
def health(request: Request) -> dict:
    ok, detail = request.app.state.llm.health()
    return {"status": "ok", "model": settings.ollama_model, "llm_ok": bool(ok), "llm_detail": str(detail)}


def _handle_locked(assistant, message: str) -> AssistantReply:
    with _chat_lock:
        return assistant.handle(message)


@app.post("/chat", response_model=AssistantReply)
async def chat(body: ChatRequest, request: Request) -> AssistantReply:
    # handle() is blocking (model inference), so run it off the event loop.
    return await run_in_threadpool(_handle_locked, request.app.state.assistant, body.message)


@app.get("/reminders", response_model=list[ReminderOut])
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


@app.get("/notifications", response_model=list[NotificationOut])
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


@app.post("/notifications/{notification_id}/read")
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
    uvicorn.run("app.main:app", host=settings.host, port=settings.port)
