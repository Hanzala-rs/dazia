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


class FakeScheduler:
    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        pass

    def shutdown(self):
        pass


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "OllamaClient", FakeLLM)
    monkeypatch.setattr(main, "Assistant", FakeAssistant)
    monkeypatch.setattr(main, "ReminderScheduler", FakeScheduler)
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


class FakeConn:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


@pytest.fixture
def fake_conn(monkeypatch):
    conn = FakeConn()
    monkeypatch.setattr(main.db, "connect", lambda path: conn)
    return conn


def test_notifications_list(client, monkeypatch, fake_conn):
    calls = {}
    created = datetime(2026, 10, 15, 4, 0, tzinfo=timezone.utc)

    def fake_list(conn, unread_only=True, limit=50):
        calls.update(unread_only=unread_only, limit=limit)
        return [
            db.Notification(2, 7, "Missed reminder", "Call Ali — due Thu 15 Oct, 9:00 AM",
                            db.KIND_MISSED, created, None),
            db.Notification(1, None, "Reminder", "Tea", db.KIND_REMINDER, created, created),
        ]

    monkeypatch.setattr(main.db, "list_notifications", fake_list)
    res = client.get("/notifications?unread_only=false&limit=5")
    assert res.status_code == 200
    assert res.json() == [
        {"id": 2, "reminder_id": 7, "title": "Missed reminder", "body": "Call Ali — due Thu 15 Oct, 9:00 AM",
         "kind": "missed", "created_at": "2026-10-15T04:00:00+00:00", "read_at": None},
        {"id": 1, "reminder_id": None, "title": "Reminder", "body": "Tea", "kind": "reminder",
         "created_at": "2026-10-15T04:00:00+00:00", "read_at": "2026-10-15T04:00:00+00:00"},
    ]
    assert calls == {"unread_only": False, "limit": 5}
    assert fake_conn.closed


def test_notifications_defaults(client, monkeypatch, fake_conn):
    calls = {}
    monkeypatch.setattr(main.db, "list_notifications",
                        lambda conn, unread_only=True, limit=50: calls.update(u=unread_only, l=limit) or [])
    assert client.get("/notifications").json() == []
    assert calls == {"u": True, "l": 20}


@pytest.mark.parametrize("limit", [0, 101, -1, "abc"])
def test_notifications_bad_limit_rejected(client, limit):
    assert client.get(f"/notifications?limit={limit}").status_code == 422


@pytest.mark.parametrize("found", [True, False])
def test_notification_mark_read(client, monkeypatch, fake_conn, found):
    seen = []
    monkeypatch.setattr(main.db, "mark_notification_read", lambda conn, nid: seen.append(nid) or found)
    res = client.post("/notifications/3/read")
    assert res.status_code == 200
    assert res.json() == {"ok": found}
    assert seen == [3]
    assert fake_conn.closed


def test_notification_mark_read_bad_id(client):
    assert client.post("/notifications/abc/read").status_code == 422
