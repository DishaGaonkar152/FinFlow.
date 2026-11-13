import getpass
import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from fastapi import HTTPException
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, RedirectResponse


COOKIE_NAME = "finflow_session"
SESSION_SECONDS = 8 * 60 * 60
PASSWORD_ITERATIONS = 600_000
PUBLIC_PATHS = {"/login", "/static/login.css", "/static/login.js", "/api/auth/session", "/api/auth/login", "/api/auth/setup"}


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=128)


class OwnerSetupRequest(LoginRequest):
    password: str = Field(min_length=12, max_length=128)


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt}${digest.hex()}"


def check_password(password: str, encoded: str) -> bool:
    _, iterations, salt, expected = encoded.split("$")
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iterations))
    return hmac.compare_digest(digest.hex(), expected)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AuthStore:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS owner (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    username TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    failed_attempts INTEGER NOT NULL DEFAULT 0,
                    locked_until REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    csrf_token TEXT NOT NULL,
                    expires_at REAL NOT NULL
                );
            """)
        self.dummy_hash = hash_password(secrets.token_urlsafe(32))

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def configured(self) -> bool:
        with self.connect() as connection:
            return connection.execute("SELECT 1 FROM owner WHERE id=1").fetchone() is not None

    def initialize_owner(self, username: str, password: str) -> None:
        if not username.strip() or len(username) > 80:
            raise ValueError("Use a username between 1 and 80 characters.")
        if not 12 <= len(password) <= 128:
            raise ValueError("Use a password between 12 and 128 characters.")
        encoded = hash_password(password)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM owner WHERE id=1").fetchone():
                raise ValueError("A FinFlow owner is already configured. Existing credentials were not changed.")
            connection.execute("INSERT INTO owner(id, username, password_hash) VALUES (1, ?, ?)", (username.strip(), encoded))

    def authenticate(self, username: str, password: str) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            owner = connection.execute("SELECT * FROM owner WHERE id=1").fetchone()
            if not owner:
                raise HTTPException(status_code=503, detail="Create your private login on the sign-in page first.")
            now = time.time()
            if owner["locked_until"] > now:
                raise HTTPException(status_code=429, detail="Too many attempts. Wait one minute before trying again.", headers={"Retry-After": "60"})
            same_user = hmac.compare_digest(username.encode("utf-8"), owner["username"].encode("utf-8"))
            valid_password = check_password(password, owner["password_hash"] if same_user else self.dummy_hash)
            if same_user and valid_password:
                connection.execute("UPDATE owner SET failed_attempts=0, locked_until=0 WHERE id=1")
                return
            failures = (owner["failed_attempts"] if not owner["locked_until"] else 0) + 1
            connection.execute("UPDATE owner SET failed_attempts=?, locked_until=? WHERE id=1", (failures, now + 60 if failures >= 5 else 0))
        raise HTTPException(status_code=401, detail="Incorrect username or password.")

    def create_session(self) -> tuple[str, str]:
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        now = time.time()
        with self.connect() as connection:
            connection.execute("DELETE FROM sessions WHERE expires_at<=?", (now,))
            connection.execute("INSERT INTO sessions VALUES (?, ?, ?)", (token_hash(token), csrf, now + SESSION_SECONDS))
        return token, csrf

    def get_session(self, token: str | None) -> dict | None:
        if not token or len(token) > 128:
            return None
        with self.connect() as connection:
            row = connection.execute("SELECT s.csrf_token, s.expires_at, o.username FROM sessions s CROSS JOIN owner o WHERE s.token_hash=? AND s.expires_at>?", (token_hash(token), time.time())).fetchone()
        return dict(row) if row else None

    def revoke_session(self, token: str | None) -> None:
        if token:
            with self.connect() as connection:
                connection.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash(token),))


def local_request(request) -> bool:
    return (
        request.url.hostname in {"localhost", "127.0.0.1", "::1"}
        and request.client is not None
        and request.client.host in {"127.0.0.1", "::1"}
    )


def trusted_transport(request) -> bool:
    return request.url.scheme == "https" or local_request(request)


def same_origin(request) -> bool:
    origin = request.headers.get("origin", "")
    return bool(origin and origin == f"{request.url.scheme}://{request.url.netloc}" and request.headers.get("sec-fetch-site") != "cross-site")


class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, store: AuthStore):
        super().__init__(app)
        self.store = store

    async def dispatch(self, request, call_next):
        path = request.url.path
        secure_transport = trusted_transport(request)
        session = self.store.get_session(request.cookies.get(COOKIE_NAME)) if secure_transport else None
        request.state.auth_session = session
        if not secure_transport:
            response = JSONResponse({"detail": "HTTPS is required outside localhost."}, status_code=403)
        elif path == "/api/auth/setup" and not local_request(request):
            response = JSONResponse({"detail": "Create the owner account on the computer running FinFlow."}, status_code=403)
        elif path not in PUBLIC_PATHS and not session:
            if path.startswith("/api/"):
                response = JSONResponse({"detail": "Please sign in to FinFlow."}, status_code=401)
            else:
                response = RedirectResponse("/login", status_code=303)
        elif request.method not in {"GET", "HEAD", "OPTIONS"}:
            if not same_origin(request):
                response = JSONResponse({"detail": "Cross-origin requests are not allowed."}, status_code=403)
            elif request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
                response = JSONResponse({"detail": "Send JSON requests."}, status_code=415)
            elif path not in {"/api/auth/login", "/api/auth/setup"} and (not session or not hmac.compare_digest(request.headers.get("x-csrf-token", "").encode("utf-8"), session["csrf_token"].encode("utf-8"))):
                response = JSONResponse({"detail": "Refresh the page before trying again."}, status_code=403)
            else:
                response = await call_next(request)
        else:
            response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        return response


def initialize_local_owner():
    base = Path(__file__).resolve().parent.parent
    audit_path = os.getenv("AUDITOR_DB_PATH", str(base / "data" / "auditor.db"))
    store = AuthStore(os.getenv("FINFLOW_AUTH_DB_PATH", audit_path + ".auth"))
    if store.configured():
        print("A FinFlow owner is already configured. No changes were made.")
        return
    print("Create the local FinFlow owner account. Passwords are hidden and never stored in plain text.")
    username = input("Username: ").strip()
    password = getpass.getpass("Password (12–128 characters): ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        raise SystemExit("Passwords did not match. No account was created.")
    try:
        store.initialize_owner(username, password)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    print("Owner account created. Open FinFlow and sign in with your credentials.")


if __name__ == "__main__":
    initialize_local_owner()
