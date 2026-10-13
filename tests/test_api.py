"""API tests. The LLM, Assistant and DB listing are replaced with fakes so these tests
don't need Ollama or the real orchestrator."""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app import db
from app.assistant import AssistantReply


class FakeLLM:
    ok = True
    detail = "model ready"

    def __init__(self, base_url, model, timeout_s):
        pass

    def health(self):
        return self.ok, self.detail


class FakeAssistant:
    def __init__(self, llm, db_path, tz):
        self.messages = []

    def handle(self, message):
        self.messages.append(message)
        return AssistantReply(reply=f"echo: {message}", tool="create_reminder", data={"id": 1})


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "OllamaClient", FakeLLM)
    monkeypatch.setattr(main, "Assistant", FakeAssistant)
    with TestClient(main.app) as c:
        yield c


def test_health_ok(client):
    res = client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["llm_ok"] is True
    assert body["llm_detail"] == "model ready"
    assert body["model"] == main.settings.ollama_model


def test_health_llm_down(client, monkeypatch):
    monkeypatch.setattr(client.app.state.llm, "health", lambda: (False, "connection refused"))
    res = client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body["llm_ok"] is False
    assert body["llm_detail"] == "connection refused"


def test_chat_happy_path(client):
    res = client.post("/chat", json={"message": "  remind me at 5pm  "})
    assert res.status_code == 200
    assert res.json() == {
        "reply": "echo: remind me at 5pm",
        "tool": "create_reminder",
        "needs_clarification": False,
        "data": {"id": 1},
    }
    # Message reached the assistant stripped.
    assert client.app.state.assistant.messages == ["remind me at 5pm"]


@pytest.mark.parametrize("message", ["", "   \n\t "])
def test_chat_empty_message_rejected(client, message):
    res = client.post("/chat", json={"message": message})
    assert res.status_code == 422
    assert client.app.state.assistant.messages == []


def test_chat_too_long_rejected(client):
    res = client.post("/chat", json={"message": "x" * (main.settings.max_message_chars + 1)})
    assert res.status_code == 422
    assert client.app.state.assistant.messages == []


def test_chat_max_length_allowed(client):
    res = client.post("/chat", json={"message": "x" * main.settings.max_message_chars})
    assert res.status_code == 200


def test_chat_missing_field_rejected(client):
    assert client.post("/chat", json={}).status_code == 422


def test_reminders_lists_pending(client, monkeypatch):
    calls = {}

    class FakeConn:
        closed = False

        def close(self):
            FakeConn.closed = True

    def fake_list(conn, status=None):
        calls["status"] = status
        due = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
        return [db.Reminder(7, "Call Ali", due, "Asia/Karachi", db.STATUS_PENDING, due, None)]

    monkeypatch.setattr(main.db, "connect", lambda path: FakeConn())
    monkeypatch.setattr(main.db, "list_reminders", fake_list)

    res = client.get("/reminders?status=pending")
    assert res.status_code == 200
    assert res.json() == [{
        "id": 7, "title": "Call Ali", "due_utc": "2026-10-15T12:00:00+00:00",
        "tz": "Asia/Karachi", "status": "pending",
    }]
    assert calls["status"] == "pending"
    assert FakeConn.closed


def test_reminders_bad_status_rejected(client):
    assert client.get("/reminders?status=bogus").status_code == 422


def test_index_serves_html(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    assert "DAZIA" in res.text


def test_static_assets_served(client):
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200
