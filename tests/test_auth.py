"""Phase 3 login tests. Uses a temporary SQLite file and the fake LLM/Assistant/Scheduler from test_api."""
import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app import auth, db
from tests.test_api import FakeAssistant, FakeLLM, FakeScheduler

PASSPHRASE = "correct horse battery"
# Few iterations keep the tests fast; production hashes use auth.ITERATIONS.
STORED_HASH = auth.hash_passphrase(PASSPHRASE, iterations=1000)

PROTECTED = [
    ("get", "/health", None),
    ("post", "/chat", {"message": "hi"}),
    ("get", "/reminders", None),
    ("get", "/notifications", None),
    ("post", "/notifications/1/read", None),
]


def _make_client(monkeypatch, tmp_path, passphrase_hash):
    monkeypatch.setattr(main, "OllamaClient", FakeLLM)
    monkeypatch.setattr(main, "Assistant", FakeAssistant)
    monkeypatch.setattr(main, "ReminderScheduler", FakeScheduler)
    monkeypatch.setattr(main.settings, "db_path", str(tmp_path / "auth.db"))
    monkeypatch.setattr(main.settings, "host", "127.0.0.1")
    monkeypatch.setattr(main.settings, "passphrase_hash", passphrase_hash)
    monkeypatch.setattr(auth, "_failures", {})
    return TestClient(main.app)


@pytest.fixture
def client(monkeypatch, tmp_path):
    with _make_client(monkeypatch, tmp_path, STORED_HASH) as c:
        yield c


@pytest.fixture
def open_client(monkeypatch, tmp_path):
    with _make_client(monkeypatch, tmp_path, "") as c:
        yield c


def _login(client, passphrase=PASSPHRASE):
    return client.post("/login", json={"passphrase": passphrase})


def _session_rows():
    conn = db.connect(main.settings.db_path)
    try:
        return conn.execute("SELECT * FROM sessions").fetchall()
    finally:
        conn.close()


# ---- hashing -----------------------------------------------------------------------

def test_hash_format_and_round_trip():
    stored = auth.hash_passphrase("s3cret-pass")
    algorithm, iterations, salt_hex, hash_hex = stored.split("$")
    assert algorithm == "pbkdf2_sha256"
    assert int(iterations) >= 200_000
    assert len(bytes.fromhex(salt_hex)) == 16
    assert len(bytes.fromhex(hash_hex)) == 32
    assert auth.verify_passphrase("s3cret-pass", stored)
    assert not auth.verify_passphrase("wrong-pass", stored)


def test_same_passphrase_gets_different_salt():
    assert auth.hash_passphrase("abcdefgh", 1000) != auth.hash_passphrase("abcdefgh", 1000)


@pytest.mark.parametrize("bad", ["", "plain", "md5$1$aa$bb", "pbkdf2_sha256$x$aa$bb", "pbkdf2_sha256$10$zz$bb"])
def test_malformed_hash_never_verifies(bad):
    assert not auth.verify_passphrase("anything", bad)


# ---- startup safety ------------------------------------------------------------------

@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "127.0.0.2"])
def test_loopback_without_hash_is_allowed(host):
    auth.check_startup_safety(host, "")


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.20", "::", "my-laptop"])
def test_non_loopback_without_hash_refuses(host):
    with pytest.raises(RuntimeError, match="no passphrase"):
        auth.check_startup_safety(host, "")


def test_non_loopback_with_hash_is_allowed():
    auth.check_startup_safety("0.0.0.0", STORED_HASH)


def test_malformed_hash_refuses_to_start():
    with pytest.raises(RuntimeError, match="malformed"):
        auth.check_startup_safety("127.0.0.1", "not-a-hash")


def test_app_startup_refuses_lan_without_hash(monkeypatch, tmp_path):
    client = _make_client(monkeypatch, tmp_path, "")
    monkeypatch.setattr(main.settings, "host", "0.0.0.0")
    with pytest.raises(RuntimeError):
        with client:
            pass


# ---- login / logout ----------------------------------------------------------------

def test_status_before_and_after_login(client):
    assert client.get("/auth/status").json() == {"auth_required": True, "authenticated": False}
    assert _login(client).status_code == 200
    assert client.get("/auth/status").json() == {"auth_required": True, "authenticated": True}


def test_login_sets_safe_cookie_and_stores_only_hash(client):
    res = _login(client)
    assert res.status_code == 200
    assert res.json() == {"ok": True}
    cookie = res.headers["set-cookie"]
    assert cookie.startswith(f"{auth.COOKIE_NAME}=")
    lowered = cookie.lower()
    assert "httponly" in lowered
    assert "samesite=strict" in lowered
    assert "path=/" in lowered
    assert f"max-age={main.settings.session_days * 86400}" in lowered
    assert "secure" not in lowered  # plain http (Phase 5 adds HTTPS)

    token = client.cookies.get(auth.COOKIE_NAME)
    rows = _session_rows()
    assert len(rows) == 1
    assert rows[0]["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
    assert rows[0]["token_hash"] != token


def test_wrong_passphrase_401(client):
    res = _login(client, "nope nope nope")
    assert res.status_code == 401
    assert res.json() == {"detail": "Wrong passphrase"}
    assert "set-cookie" not in res.headers
    assert _session_rows() == []


def test_rate_limit_then_reset_after_success(client):
    for _ in range(auth.MAX_FAILURES):
        assert _login(client, "wrong wrong").status_code == 401
    res = _login(client)  # even the right passphrase is blocked now
    assert res.status_code == 429
    assert res.json()["detail"]

    auth._failures.clear()  # simulate the window passing
    assert _login(client).status_code == 200
    # Success wiped the counter: a fresh run of failures starts from zero.
    for _ in range(auth.MAX_FAILURES - 1):
        assert _login(client, "wrong wrong").status_code == 401
    assert _login(client).status_code == 200


def test_old_failures_expire(client, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(auth.time, "monotonic", lambda: now[0])
    for _ in range(auth.MAX_FAILURES):
        _login(client, "wrong wrong")
    assert _login(client).status_code == 429
    now[0] += auth.FAILURE_WINDOW_S + 1
    assert _login(client).status_code == 200


@pytest.mark.parametrize("method,path,body", PROTECTED)
def test_protected_routes_need_login(client, method, path, body):
    res = client.request(method.upper(), path, json=body)
    assert res.status_code == 401
    assert res.json() == {"detail": "Not logged in"}


@pytest.mark.parametrize("method,path,body", PROTECTED)
def test_protected_routes_work_after_login(client, method, path, body):
    _login(client)
    assert client.request(method.upper(), path, json=body).status_code == 200


def test_public_routes_stay_open(client):
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/auth/status").status_code == 200


def test_bogus_cookie_rejected(client):
    client.cookies.set(auth.COOKIE_NAME, "made-up-token")
    assert client.get("/health").status_code == 401


def test_logout_invalidates_session(client):
    _login(client)
    token = client.cookies.get(auth.COOKIE_NAME)
    res = client.post("/logout")
    assert res.status_code == 200
    assert res.json() == {"ok": True}
    assert _session_rows() == []
    # Even if someone kept a copy of the old cookie, it no longer works.
    client.cookies.set(auth.COOKIE_NAME, token)
    assert client.get("/health").status_code == 401


def test_expired_session_rejected_and_cleaned_up(client):
    token = "expired-token"
    conn = db.connect(main.settings.db_path)
    try:
        db.add_session(conn, hashlib.sha256(token.encode()).hexdigest(),
                       datetime.now(timezone.utc) - timedelta(minutes=1))
    finally:
        conn.close()
    client.cookies.set(auth.COOKIE_NAME, token)
    assert client.get("/health").status_code == 401
    assert _session_rows() == []


# ---- auth not configured (loopback only) -----------------------------------------------

def test_no_passphrase_means_open(open_client):
    assert open_client.get("/auth/status").json() == {"auth_required": False, "authenticated": True}
    assert open_client.post("/login", json={"passphrase": "anything"}).json() == {"ok": True}
    assert open_client.post("/logout").json() == {"ok": True}
    for method, path, body in PROTECTED:
        assert open_client.request(method.upper(), path, json=body).status_code == 200


# ---- CLI ---------------------------------------------------------------------------

def test_cli_prints_env_line(monkeypatch, capsys):
    monkeypatch.setattr(auth, "ITERATIONS", 1000)
    monkeypatch.setattr(auth.hash_passphrase, "__defaults__", (1000,))
    answers = iter([PASSPHRASE, PASSPHRASE])
    monkeypatch.setattr(auth.getpass, "getpass", lambda prompt="": next(answers))
    auth.main()
    out = capsys.readouterr().out
    line = next(l for l in out.splitlines() if l.startswith("DAZIA_PASSPHRASE_HASH="))
    assert auth.verify_passphrase(PASSPHRASE, line.split("=", 1)[1])
    assert PASSPHRASE not in out


@pytest.mark.parametrize("answers", [["short", "short"], [PASSPHRASE, PASSPHRASE + "x"]])
def test_cli_rejects_short_or_mismatch(monkeypatch, capsys, answers):
    it = iter(answers)
    monkeypatch.setattr(auth.getpass, "getpass", lambda prompt="": next(it))
    with pytest.raises(SystemExit):
        auth.main()
    assert "DAZIA_PASSPHRASE_HASH=" not in capsys.readouterr().out
