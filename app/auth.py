"""Phase 3 login for DAZIA: one passphrase, session cookies, login rate limiting.

- The passphrase is never stored; only a PBKDF2 hash lives in .env (DAZIA_PASSPHRASE_HASH).
- A login creates a random session token. The browser keeps it in an HttpOnly cookie;
  SQLite keeps only sha256(token).
- Login is required whenever DAZIA_PASSPHRASE_HASH is set. The app refuses to start on a
  non-loopback host without one (see check_startup_safety).

Generate a hash:  .venv/Scripts/python -m app.auth
"""
import getpass
import hashlib
import hmac
import ipaddress
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request, Response

from app import db
from app.config import settings

COOKIE_NAME = "dazia_session"
ITERATIONS = 300_000
MIN_PASSPHRASE_CHARS = 8
_ALGORITHM = "pbkdf2_sha256"

# Login rate limit: at most MAX_FAILURES wrong passphrases per client IP within FAILURE_WINDOW_S.
MAX_FAILURES = 5
FAILURE_WINDOW_S = 5 * 60


# ---- passphrase hashing ------------------------------------------------------------

def hash_passphrase(passphrase: str, iterations: int = ITERATIONS) -> str:
    """Return 'pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>' with a fresh random salt."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, iterations)
    return f"{_ALGORITHM}${iterations}${salt.hex()}${digest.hex()}"


def _parse_hash(stored: str) -> tuple[int, bytes, bytes] | None:
    """Split a stored hash into (iterations, salt, digest); None if it is malformed."""
    parts = stored.strip().split("$")
    if len(parts) != 4 or parts[0] != _ALGORITHM:
        return None
    try:
        iterations, salt, digest = int(parts[1]), bytes.fromhex(parts[2]), bytes.fromhex(parts[3])
    except ValueError:
        return None
    if iterations < 1 or not salt or not digest:
        return None
    return iterations, salt, digest


def verify_passphrase(passphrase: str, stored: str) -> bool:
    parsed = _parse_hash(stored)
    if parsed is None:
        return False
    iterations, salt, expected = parsed
    actual = hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


# ---- startup safety ----------------------------------------------------------------

def is_loopback(host: str) -> bool:
    if host.strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip()).is_loopback
    except ValueError:
        return False  # a hostname or 0.0.0.0-style bind we can't prove is local


def check_startup_safety(host: str, passphrase_hash: str) -> None:
    """Raise RuntimeError if starting would expose DAZIA without a login."""
    if passphrase_hash and _parse_hash(passphrase_hash) is None:
        raise RuntimeError(
            "DAZIA_PASSPHRASE_HASH is malformed. Generate a new one with: .venv\\Scripts\\python -m app.auth"
        )
    if not passphrase_hash and not is_loopback(host):
        raise RuntimeError(
            f"Refusing to start: DAZIA_HOST={host} makes DAZIA reachable from other devices, "
            "but no passphrase is set. Run '.venv\\Scripts\\python -m app.auth', put the printed "
            "DAZIA_PASSPHRASE_HASH=... line in .env, then start again (or set DAZIA_HOST=127.0.0.1)."
        )


def auth_required() -> bool:
    return bool(settings.passphrase_hash)


# Headers added by a forwarding proxy such as `tailscale serve`. Remote requests then arrive from
# 127.0.0.1, so the startup host check can't see them; this catches them per request instead.
_PROXY_HEADERS = ("x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "tailscale-user-login")


def is_proxied(request: Request) -> bool:
    return any(name in request.headers for name in _PROXY_HEADERS)


# ---- sessions ----------------------------------------------------------------------

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session() -> str:
    """Store a new session and return its raw token (goes only into the cookie)."""
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    conn = db.connect(settings.db_path)
    try:
        db.delete_expired_sessions(conn, now)  # opportunistic cleanup
        db.add_session(conn, _token_hash(token), now + timedelta(days=settings.session_days))
    finally:
        conn.close()
    return token


def session_is_valid(token: str | None) -> bool:
    if not token:
        return False
    now = datetime.now(timezone.utc)
    conn = db.connect(settings.db_path)
    try:
        if db.get_session_valid(conn, _token_hash(token), now):
            return True
        db.delete_expired_sessions(conn, now)  # the token may simply have expired
        return False
    finally:
        conn.close()


def end_session(token: str | None) -> None:
    if not token:
        return
    conn = db.connect(settings.db_path)
    try:
        db.delete_session(conn, _token_hash(token))
    finally:
        conn.close()


def is_authenticated(request: Request) -> bool:
    return not auth_required() or session_is_valid(request.cookies.get(COOKIE_NAME))


def require_auth(request: Request) -> None:
    """FastAPI dependency for every route that touches user data."""
    if not is_authenticated(request):
        raise HTTPException(status_code=401, detail="Not logged in")


def set_session_cookie(response: Response, request: Request, token: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=settings.session_days * 24 * 60 * 60,
        path="/",
        httponly=True,  # page JavaScript can't read it
        samesite="strict",  # not sent on requests started by other sites
        secure=request.url.scheme == "https",  # plain-http LAN must still work until Phase 5 HTTPS
    )


def clear_session_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(
        COOKIE_NAME, path="/", httponly=True, samesite="strict", secure=request.url.scheme == "https"
    )


# ---- login rate limiting (in memory; resets when the server restarts) -----------------

_failures: dict[str, list[float]] = {}
_failures_lock = threading.Lock()


def _recent_failures(ip: str, now: float) -> list[float]:
    """Failures for ip inside the window (caller holds the lock). Drops old entries."""
    recent = [t for t in _failures.get(ip, []) if now - t < FAILURE_WINDOW_S]
    if recent:
        _failures[ip] = recent
    else:
        _failures.pop(ip, None)
    return recent


def too_many_failures(ip: str) -> bool:
    with _failures_lock:
        return len(_recent_failures(ip, time.monotonic())) >= MAX_FAILURES


def record_failure(ip: str) -> None:
    with _failures_lock:
        now = time.monotonic()
        _recent_failures(ip, now)
        _failures.setdefault(ip, []).append(now)


def reset_failures(ip: str) -> None:
    with _failures_lock:
        _failures.pop(ip, None)


# ---- CLI: print a hash line for .env ---------------------------------------------------

def main() -> None:
    first = getpass.getpass(f"New DAZIA passphrase (at least {MIN_PASSPHRASE_CHARS} characters): ")
    if len(first) < MIN_PASSPHRASE_CHARS:
        raise SystemExit(f"Too short: use at least {MIN_PASSPHRASE_CHARS} characters.")
    if getpass.getpass("Type it again: ") != first:
        raise SystemExit("The two passphrases did not match. Nothing was created.")
    print("\nAdd this line to your .env file (it is a hash, not the passphrase):\n")
    print(f"DAZIA_PASSPHRASE_HASH={hash_passphrase(first)}")


if __name__ == "__main__":
    main()
