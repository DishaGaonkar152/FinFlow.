import hashlib
import importlib
import os
import secrets
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from http.cookies import SimpleCookie
from io import BytesIO
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from docx import Document
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pypdf import PdfReader

from app.auth import AuthStore, COOKIE_NAME, SESSION_SECONDS, check_password, hash_password


def database_dump(path):
    # sqlite3's transaction context manager does not close Windows file handles.
    with closing(sqlite3.connect(path)) as connection:
        return "\n".join(connection.iterdump())


class AuthStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="finflow-auth-store-test-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.path = str(self.directory / "auth.sqlite")
        self.store = AuthStore(self.path)
        self.username = "temporary-owner"
        self.password = secrets.token_urlsafe(24)

    def initialize(self):
        self.store.initialize_owner(self.username, self.password)

    def owner(self):
        with self.store.connect() as connection:
            row = connection.execute("SELECT * FROM owner").fetchone()
        return dict(row) if row else None

    def test_password_hashes_use_random_salts_and_verifiable_pbkdf2(self):
        password = self.password + "\u00e9"
        first, second = hash_password(password), hash_password(password)
        self.assertNotEqual(first, second)
        for encoded in (first, second):
            algorithm, iterations, salt, digest = encoded.split("$")
            self.assertEqual(algorithm, "pbkdf2_sha256")
            self.assertGreaterEqual(int(iterations), 600_000)
            self.assertEqual(len(bytes.fromhex(salt)), 16)
            self.assertEqual(len(bytes.fromhex(digest)), 32)
            expected = hashlib.pbkdf2_hmac(
                "sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iterations)
            ).hex()
            self.assertEqual(digest, expected)
            self.assertTrue(check_password(password, encoded))
            self.assertFalse(check_password(password + "wrong", encoded))
            self.assertNotIn(password, encoded)

    def test_owner_password_and_username_boundaries(self):
        for username, password in (
            (self.username, "x" * 11), (self.username, "x" * 129),
            ("", self.password), (" \t ", self.password), ("u" * 81, self.password),
        ):
            with self.subTest(username_length=len(username), password_length=len(password)):
                with self.assertRaises(ValueError):
                    self.store.initialize_owner(username, password)
                self.assertFalse(self.store.configured())
        for size in (12, 128):
            with self.subTest(valid_password_length=size):
                store = AuthStore(str(self.directory / f"boundary-{size}.sqlite"))
                password = (self.password * 6)[:size]
                store.initialize_owner("u" * 80, password)
                self.assertTrue(store.configured())
                self.assertIsNone(store.authenticate("u" * 80, password))

    def test_owner_hash_has_no_plaintext_and_duplicate_initialization_is_unchanged(self):
        self.store.initialize_owner("  " + self.username + "  ", self.password)
        owner = self.owner()
        self.assertEqual(owner["username"], self.username)
        self.assertNotIn(self.password, owner["password_hash"])
        self.assertTrue(check_password(self.password, owner["password_hash"]))
        before = database_dump(self.path)
        before_bytes = Path(self.path).read_bytes()
        replacement_password = secrets.token_urlsafe(24)
        with self.assertRaisesRegex(ValueError, "already configured"):
            self.store.initialize_owner("replacement-owner", replacement_password)
        self.assertEqual(self.owner(), owner)
        self.assertEqual(database_dump(self.path), before)
        self.assertEqual(Path(self.path).read_bytes(), before_bytes)
        self.assertNotIn(self.password.encode(), before_bytes)
        self.assertNotIn(replacement_password.encode(), Path(self.path).read_bytes())
        self.assertIsNone(self.store.authenticate(self.username, self.password))
        with self.store.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM owner").fetchone()[0], 1)

    def test_unconfigured_authentication_cannot_create_an_owner(self):
        before = database_dump(self.path)
        with self.assertRaises(HTTPException) as caught:
            self.store.authenticate(self.username, self.password)
        self.assertEqual(caught.exception.status_code, 503)
        self.assertFalse(self.store.configured())
        self.assertEqual(database_dump(self.path), before)

    def test_sessions_store_only_sha256_tokens_and_revoke_individually(self):
        self.initialize()
        with patch("app.auth.time.time", return_value=1_000.0):
            first = self.store.create_session()
            second = self.store.create_session()
            self.assertIsInstance(first, tuple)
            self.assertEqual(len(first), 2)
            token, csrf = first
            self.assertNotEqual(token, csrf)
            self.assertNotEqual(first[0], second[0])
            self.assertNotEqual(first[1], second[1])
            self.assertEqual(SESSION_SECONDS, 28_800)
            with self.store.connect() as connection:
                rows = [dict(row) for row in connection.execute("SELECT * FROM sessions")]
            self.assertEqual({row["token_hash"] for row in rows}, {
                hashlib.sha256(value.encode()).hexdigest() for value in (token, second[0])
            })
            for row in rows:
                self.assertEqual(row["expires_at"], 1_000.0 + 28_800)
            self.assertEqual(self.store.get_session(token), {
                "username": self.username, "csrf_token": csrf, "expires_at": 29_800.0,
            })
            dump = database_dump(self.path)
            for value in (self.password, token, second[0]):
                self.assertNotIn(value, dump)
                self.assertNotIn(value.encode(), Path(self.path).read_bytes())
            for invalid in (None, "", "unknown-token", "x" * 129, "\u00e9" * 129):
                self.assertIsNone(self.store.get_session(invalid))
            self.store.revoke_session(None)
            self.store.revoke_session("not-a-session")
            self.assertIsNotNone(self.store.get_session(token))
            self.store.revoke_session(token)
            self.store.revoke_session(token)
            self.assertIsNone(self.store.get_session(token))
            self.assertIsNotNone(self.store.get_session(second[0]))

    def test_session_expiry_is_exactly_eight_hours_and_new_sessions_purge_expired_rows(self):
        self.initialize()
        with patch("app.auth.time.time", return_value=2_000.0) as clock:
            token, _ = self.store.create_session()
            deadline = 2_000.0 + 28_800
            clock.return_value = deadline - 0.001
            self.assertIsNotNone(self.store.get_session(token))
            clock.return_value = deadline
            self.assertIsNone(self.store.get_session(token))
            clock.return_value = deadline + 0.001
            self.assertIsNone(self.store.get_session(token))
            replacement, _ = self.store.create_session()
            with self.store.connect() as connection:
                rows = connection.execute("SELECT token_hash FROM sessions").fetchall()
            self.assertEqual([row[0] for row in rows], [hashlib.sha256(replacement.encode()).hexdigest()])

    def test_owner_sessions_and_revocation_persist_across_store_restart(self):
        self.initialize()
        with patch("app.auth.time.time", return_value=3_000.0) as clock:
            active, csrf = self.store.create_session()
            revoked, _ = self.store.create_session()
            self.store.revoke_session(revoked)
            before = database_dump(self.path)
            restarted = AuthStore(self.path)
            self.assertEqual(database_dump(self.path), before)
            self.assertTrue(restarted.configured())
            self.assertIsNone(restarted.authenticate(self.username, self.password))
            self.assertEqual(restarted.get_session(active)["csrf_token"], csrf)
            self.assertIsNone(restarted.get_session(revoked))
            clock.return_value = 3_000.0 + 28_800
            self.assertIsNone(AuthStore(self.path).get_session(active))

    def test_unknown_username_and_wrong_password_have_identical_errors(self):
        self.initialize()
        errors = []
        for username, password in ((self.username, "incorrect"), ("unknown-owner", self.password)):
            with self.assertRaises(HTTPException) as caught:
                self.store.authenticate(username, password)
            errors.append((caught.exception.status_code, caught.exception.detail))
        self.assertEqual(errors, [(401, "Incorrect username or password.")] * 2)
        self.assertEqual(self.owner()["failed_attempts"], 2)

    def test_five_failures_lock_for_sixty_seconds_then_success_resets(self):
        self.initialize()
        with patch("app.auth.time.time", return_value=4_000.0) as clock:
            for attempt in range(1, 6):
                with self.subTest(attempt=attempt):
                    with self.assertRaises(HTTPException) as caught:
                        self.store.authenticate("unknown-owner" if attempt % 2 else self.username, "wrong")
                    self.assertEqual(caught.exception.status_code, 401)
                    self.assertEqual(self.owner()["failed_attempts"], attempt)
            self.assertEqual(self.owner()["locked_until"], 4_060.0)
            locked_owner = self.owner()
            clock.return_value = 4_059.999
            for username, password in ((self.username, self.password), ("unknown-owner", "wrong")):
                with self.assertRaises(HTTPException) as caught:
                    self.store.authenticate(username, password)
                self.assertEqual(caught.exception.status_code, 429)
                self.assertEqual(caught.exception.headers, {"Retry-After": "60"})
                self.assertEqual(self.owner(), locked_owner)
            clock.return_value = 4_060.0
            self.assertIsNone(self.store.authenticate(self.username, self.password))
            self.assertEqual(self.owner()["failed_attempts"], 0)
            self.assertEqual(self.owner()["locked_until"], 0)

    def test_failure_after_expired_lock_restarts_count_and_success_breaks_streak(self):
        self.initialize()
        with patch("app.auth.time.time", return_value=5_000.0) as clock:
            for _ in range(5):
                with self.assertRaises(HTTPException):
                    self.store.authenticate(self.username, "wrong")
            clock.return_value = 5_060.0
            with self.assertRaises(HTTPException) as caught:
                self.store.authenticate(self.username, "wrong")
            self.assertEqual(caught.exception.status_code, 401)
            self.assertEqual(self.owner()["failed_attempts"], 1)
            self.assertEqual(self.owner()["locked_until"], 0)
            self.store.authenticate(self.username, self.password)
            with self.assertRaises(HTTPException):
                self.store.authenticate(self.username, "wrong")
            self.assertEqual(self.owner()["failed_attempts"], 1)


class DemoMCPFixture:
    def __init__(self, sec):
        self.sec = sec
        self.calls = []

    def list_tools(self):
        return [
            {"name": "retrieve_10k_filing", "description": "Local DEMO fixture"},
            {"name": "lookup_xbrl_company_facts", "description": "Local DEMO facts"},
        ]

    def call(self, name, arguments):
        if arguments != {"ticker": "DEMO"}:
            raise AssertionError("Authentication tests must never retrieve live filings")
        self.calls.append(name)
        if name == "retrieve_10k_filing":
            filing = self.sec.retrieve_filing("DEMO")
            return {
                "company": filing.company, "accession": filing.accession,
                "report_date": filing.report_date, "url": filing.url,
                "html": filing.html, "offline": filing.offline,
            }
        if name == "lookup_xbrl_company_facts":
            return {"ticker": "DEMO", "company_facts": self.sec.lookup_company_facts("DEMO")}
        raise AssertionError(f"Unexpected DEMO tool: {name}")


class AuthAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Refuse a previously imported main that might point at actual project data.
        if "app.main" in sys.modules:
            raise RuntimeError("Run test_auth.py in a fresh process; app.main is already imported")
        temporary = tempfile.TemporaryDirectory(prefix="finflow-auth-api-test-")
        cls.addClassCleanup(temporary.cleanup)
        cls.directory = Path(temporary.name)
        cls.audit_path = str(cls.directory / "audits.sqlite")
        cls.agent_path = str(cls.directory / "agent.sqlite")
        cls.auth_path = str(cls.directory / "auth.sqlite")
        environment = patch.dict(os.environ, {
            "AUDITOR_DB_PATH": cls.audit_path,
            "AUDITOR_AGENT_DB_PATH": cls.agent_path,
            "FINFLOW_AUTH_DB_PATH": cls.auth_path,
            "LLM_API_KEY": "", "OPENAI_API_KEY": "",
            "MPLCONFIGDIR": str(cls.directory),
        })
        environment.start()
        cls.addClassCleanup(environment.stop)
        cls.main = importlib.import_module("app.main")
        cls.addClassCleanup(cls.remove_main_import)
        # TestClient's lifespan would close the shared graph after each test.
        cls.addClassCleanup(cls.main.workflow.close)
        if cls.main.database.path != cls.audit_path or cls.main.auth_store.path != cls.auth_path:
            raise AssertionError("Application stores escaped the temporary directory")
        checkpoint_path = cls.main.workflow.agent._checkpoint_connection.execute(
            "PRAGMA database_list"
        ).fetchone()[2]
        if Path(checkpoint_path).resolve() != Path(cls.agent_path).resolve():
            raise AssertionError("Agent checkpoints escaped the temporary directory")
        if cls.main.workflow.explainer.configured or cls.main.workflow.assistant.configured:
            raise AssertionError("Test providers must be disabled")
        cls.username = "temporary-api-owner"
        cls.password = secrets.token_urlsafe(24)
        cls.main.auth_store.initialize_owner(cls.username, cls.password)
        with cls.main.auth_store.connect() as connection:
            cls.owner_row = tuple(connection.execute("SELECT * FROM owner").fetchone())
        cls.demo_mcp = DemoMCPFixture(cls.main.workflow.sec)
        mcp_patch = patch.object(cls.main.workflow, "mcp", cls.demo_mcp)
        mcp_patch.start()
        cls.addClassCleanup(mcp_patch.stop)

    @classmethod
    def remove_main_import(cls):
        if sys.modules.get("app.main") is cls.main:
            sys.modules.pop("app.main")
            package = sys.modules.get("app")
            if getattr(package, "main", None) is cls.main:
                delattr(package, "main")

    def setUp(self):
        with self.main.auth_store.connect() as connection:
            connection.execute("DELETE FROM sessions")
            connection.execute("DELETE FROM owner")
            connection.execute("INSERT INTO owner VALUES (?, ?, ?, ?, ?)", self.owner_row)
        self.demo_mcp.calls.clear()
        self.client = self.new_client()

    def new_client(self, base_url="http://localhost", peer=("127.0.0.1", 50_000)):
        client = TestClient(self.main.app, base_url=base_url, follow_redirects=False, client=peer)
        self.addCleanup(client.close)
        return client

    def assert_response(self, response, status):
        self.assertEqual(response.status_code, status, response.text[:500])
        self.assertEqual(response.headers.get("cache-control"), "no-store")
        return response

    def login(self, client=None):
        client = client or self.client
        response = client.post("/api/auth/login", json={
            "username": self.username, "password": self.password,
        }, headers={"Origin": str(client.base_url).rstrip("/")})
        self.assert_response(response, 200)
        self.assertEqual(response.json(), {"authenticated": True})
        token = client.cookies.get(COOKIE_NAME)
        self.assertTrue(token)
        session = self.assert_response(client.get("/api/auth/session"), 200).json()
        self.assertEqual(set(session), {"authenticated", "username", "csrf_token"})
        self.assertTrue(session["authenticated"])
        self.assertEqual(session["username"], self.username)
        self.assertTrue(session["csrf_token"])
        self.assertNotIn(token, response.text + str(session))
        return token, session["csrf_token"]

    def unsafe_headers(self, csrf, client=None):
        client = client or self.client
        return {"Origin": str(client.base_url).rstrip("/"), "X-CSRF-Token": csrf}

    def financial_snapshot(self):
        return database_dump(self.audit_path), database_dump(self.agent_path)

    @contextmanager
    def setup_state(self, name="first-run"):
        # Replace both references, never the workflow or its temporary financial stores.
        from app.auth import AuthMiddleware
        self.assert_response(self.client.get("/api/auth/session"), 200)
        middleware = self.main.app.middleware_stack
        while not isinstance(middleware, AuthMiddleware):
            middleware = middleware.app
        store = AuthStore(str(self.directory / f"{self._testMethodName}-{name}.sqlite"))
        self.assertFalse(store.configured())
        with patch.object(middleware, "store", store), patch.object(self.main, "auth_store", store):
            yield store

    def create_demo(self, csrf):
        response = self.client.post("/api/runs", json={"ticker": "DEMO"}, headers=self.unsafe_headers(csrf))
        run = self.assert_response(response, 200).json()
        self.assertEqual(run["ticker"], "DEMO")
        self.assertEqual(run["status"], "awaiting_review", run.get("error"))
        self.assertTrue(run["state"]["filing"]["offline"])
        self.assertEqual(len(run["state"]["metrics"]), 8)
        self.assertEqual(len(run["reviews"]), 4)
        self.assertEqual(sum(m["status"] == "auto_approved" for m in run["state"]["metrics"]), 4)
        self.assertTrue(all(m["source_snippet"] for m in run["state"]["metrics"]))
        self.assertTrue(all(m["explanation"]["generated_by"] == "rule_based_fallback" for m in run["state"]["metrics"]))
        return run

    def complete_demo(self, run, csrf):
        for review in run["reviews"]:
            correcting = review["metric_key"] == "total_liabilities"
            response = self.client.post(
                f"/api/runs/{run['id']}/reviews/{review['id']}",
                json={"decision": "correct" if correcting else "approve",
                      "corrected_value": 30_000_000 if correcting else None,
                      "reviewer": "temporary-test-reviewer", "note": "Temporary DEMO decision"},
                headers=self.unsafe_headers(csrf),
            )
            run = self.assert_response(response, 200).json()
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["state"]["report"]["accuracy_percentage"], 100.0)
        self.assertEqual(run["state"]["report"]["automatically_approved"], 4)
        self.assertEqual(run["state"]["report"]["completed_reviews"], 4)
        self.assertEqual(run["state"]["report"]["pending_reviews"], 0)
        self.assertEqual(len(run["audit_log"]), 4)
        self.assertTrue(run["audit_verification"]["valid"])
        self.assertEqual(self.demo_mcp.calls, ["retrieve_10k_filing", "lookup_xbrl_company_facts"])
        return run

    def test_anonymous_session_and_login_assets_are_the_only_public_pages(self):
        response = self.assert_response(self.client.get("/api/auth/session"), 200)
        self.assertEqual(response.json(), {"authenticated": False, "configured": True})
        self.assertNotIn("set-cookie", response.headers)
        for path in ("/login", "/static/login.css", "/static/login.js"):
            with self.subTest(path=path):
                self.assert_response(self.client.get(path), 200)
        self.assertFalse(self.client.cookies)

    def test_all_dashboard_static_assets_and_documentation_require_authentication(self):
        paths = ["/", "/static", "/static/", "/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"]
        paths.extend(
            "/static/" + path.name for path in (self.main.BASE_DIR / "static").iterdir()
            if path.is_file() and path.name not in {"login.css", "login.js"}
        )
        self.assertIn("/static/index.html", paths)
        for path in paths:
            with self.subTest(path=path):
                response = self.assert_response(self.client.get(path), 303)
                self.assertEqual(response.headers["location"], "/login")
                self.assertNotIn("Demo Manufacturing", response.text)

    def test_every_existing_api_and_report_download_requires_authentication(self):
        before = self.financial_snapshot()
        paths = [
            "/api/health", "/api/mcp/tools", "/api/companies", "/api/runs",
            "/api/runs/not-a-run", "/api/runs/not-a-run/audit/verify",
            "/api/runs/not-a-run/report?format=pdf", "/api/runs/not-a-run/report?format=docx",
        ]
        for path in paths:
            with self.subTest(path=path):
                response = self.assert_response(self.client.get(path), 401)
                self.assertEqual(response.json(), {"detail": "Please sign in to FinFlow."})
        for path, payload in (
            ("/api/runs", {"ticker": "DEMO"}),
            ("/api/runs/not-a-run/reviews/not-a-review", {"decision": "approve"}),
            ("/api/assistant", {"message": "Show finances"}),
            ("/api/auth/logout", {}),
        ):
            with self.subTest(method="POST", path=path):
                self.assert_response(self.client.post(path, json=payload, headers={"Origin": "http://localhost"}), 401)
        for method in ("HEAD", "OPTIONS"):
            self.assert_response(self.client.request(method, "/api/health"), 401)
        self.assertEqual(self.financial_snapshot(), before)
        self.assertEqual(self.demo_mcp.calls, [])

    def test_no_general_signup_or_unconfigured_login_bypass(self):
        with self.main.auth_store.connect() as connection:
            connection.execute("DELETE FROM owner")
        before = database_dump(self.auth_path)
        self.assertEqual(self.assert_response(self.client.get("/api/auth/session"), 200).json(), {
            "authenticated": False, "configured": False,
        })
        self.assert_response(self.client.get("/login"), 200)
        for path in ("/api/auth/initialize", "/api/auth/register", "/api/auth/signup", "/api/auth/owner"):
            with self.subTest(path=path):
                self.assert_response(self.client.post(path, json={
                    "username": self.username, "password": self.password,
                }, headers={"Origin": "http://localhost"}), 401)
        response = self.client.post("/api/auth/login", json={
            "username": self.username, "password": self.password,
        }, headers={"Origin": "http://localhost"})
        self.assert_response(response, 503)
        detail = response.json()["detail"].lower().replace("set up", "setup").replace("set-up", "setup")
        self.assertTrue(any(word in detail for word in ("setup", "create", "enroll")), detail)
        self.assertTrue(any(word in detail for word in ("page", "screen", "browser")), detail)
        self.assertNotIn("terminal", detail)
        self.assertNotIn("set-cookie", response.headers)
        self.assertFalse(self.main.auth_store.configured())
        self.assertEqual(database_dump(self.auth_path), before)
        self.assert_response(self.client.get("/"), 303)
        self.assert_response(self.client.get("/api/runs"), 401)

    def test_local_setup_normalizes_owner_hashes_password_and_enters_workspace(self):
        origins = (("http://localhost", "127.0.0.1"), ("http://127.0.0.1", "127.0.0.1"),
                   ("http://[::1]", "::1"), ("https://localhost", "127.0.0.1"))
        for index, (origin, address) in enumerate(origins):
            with self.subTest(origin=origin), self.setup_state(str(index)) as store:
                client = self.new_client(origin, peer=(address, 50_000))
                self.assertEqual(self.assert_response(client.get("/api/auth/session"), 200).json(), {
                    "authenticated": False, "configured": False,
                })
                self.assert_response(client.get("/login"), 200)
                before = self.financial_snapshot()
                password = self.password + " \u00e9 "
                with patch("app.auth.time.time", return_value=40_000.0):
                    response = client.post("/api/auth/setup", json={
                        "username": " \t" + self.username + " \n", "password": password,
                    }, headers={"Origin": origin})
                    self.assert_response(response, 200)
                    self.assertEqual(response.json(), {"authenticated": True})
                    token = client.cookies.get(COOKIE_NAME)
                    self.assertTrue(token)
                    cookie = SimpleCookie()
                    cookie.load(response.headers["set-cookie"])
                    morsel = cookie[COOKIE_NAME]
                    self.assertEqual(bool(morsel["secure"]), origin.startswith("https://"))
                    self.assertTrue(morsel["httponly"])
                    self.assertEqual(morsel["samesite"].lower(), "strict")
                    self.assertEqual(morsel["max-age"], "28800")
                    self.assertEqual(morsel["path"], "/")
                    self.assertFalse(morsel["domain"])
                    session = self.assert_response(client.get("/api/auth/session"), 200).json()
                    self.assertEqual(set(session), {"authenticated", "username", "csrf_token"})
                    self.assertTrue(session["authenticated"])
                    self.assertEqual(session["username"], self.username)
                    self.assertTrue(session["csrf_token"])
                    self.assertNotEqual(token, session["csrf_token"])
                    self.assertNotIn(token, response.text + str(session))
                    self.assertNotIn(password, response.text + str(session))
                    with store.connect() as connection:
                        owners = [dict(row) for row in connection.execute("SELECT * FROM owner")]
                        sessions = [dict(row) for row in connection.execute("SELECT * FROM sessions")]
                    self.assertEqual(len(owners), 1)
                    self.assertEqual(owners[0]["username"], self.username)
                    self.assertTrue(check_password(password, owners[0]["password_hash"]))
                    self.assertFalse(check_password(password.strip(), owners[0]["password_hash"]))
                    self.assertEqual(sessions, [{
                        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
                        "csrf_token": session["csrf_token"], "expires_at": 40_000.0 + 28_800,
                    }])
                    self.assertEqual(store.get_session(token)["csrf_token"], session["csrf_token"])
                    self.assert_response(client.get("/"), 200)
                    self.assert_response(client.get("/api/health"), 200)
                    self.assertEqual(self.assert_response(client.get("/login"), 303).headers["location"], "/")
                for secret in (password, token):
                    self.assertNotIn(secret, database_dump(store.path))
                    self.assertNotIn(secret.encode(), Path(store.path).read_bytes())
                self.assertEqual(self.financial_snapshot(), before)
                self.assertEqual(self.demo_mcp.calls, [])

    def test_setup_accepts_password_and_username_boundaries_in_separate_stores(self):
        for size in (12, 128):
            with self.subTest(password_length=size), self.setup_state(str(size)) as store:
                client = self.new_client()
                username = "u" * (1 if size == 12 else 80)
                password = (self.password * 6)[:size]
                self.assertEqual(len(password), size)
                before = self.financial_snapshot()
                response = client.post("/api/auth/setup", json={
                    "username": username, "password": password,
                }, headers={"Origin": "http://localhost"})
                self.assert_response(response, 200)
                self.assertEqual(response.json(), {"authenticated": True})
                self.assertIsNone(store.authenticate(username, password))
                session = self.assert_response(client.get("/api/auth/session"), 200).json()
                self.assertEqual(session["username"], username)
                self.assertEqual(self.financial_snapshot(), before)

    def test_setup_invalid_models_and_whitespace_leave_no_owner_or_session(self):
        with self.setup_state() as store:
            before = database_dump(store.path), Path(store.path).read_bytes(), self.financial_snapshot()
            invalid = [
                ({}, 422), ({"username": self.username}, 422), ({"password": self.password}, 422),
                ({"username": "", "password": self.password}, 422),
                ({"username": "u" * 81, "password": self.password}, 422),
                ({"username": None, "password": self.password}, 422),
                ({"username": [self.username], "password": self.password}, 422),
                ({"username": self.username, "password": ""}, 422),
                ({"username": self.username, "password": self.password[:11]}, 422),
                ({"username": self.username, "password": (self.password * 6)[:129]}, 422),
                ({"username": self.username, "password": None}, 422),
                ({"username": self.username, "password": [self.password]}, 422),
                ({"username": " \t\n ", "password": self.password}, 400),
                ({"username": "\u00a0\u2003", "password": self.password}, 400),
            ]
            for payload, status in invalid:
                with self.subTest(fields=list(payload), status=status,
                                  username_length=len(payload.get("username") or ""),
                                  password_length=len(payload.get("password") or "")):
                    response = self.client.post("/api/auth/setup", json=payload, headers={"Origin": "http://localhost"})
                    self.assert_response(response, status)
                    self.assertNotIn("set-cookie", response.headers)
                    self.assertFalse(self.client.cookies)
                    self.assertFalse(store.configured())
                    self.assertEqual((database_dump(store.path), Path(store.path).read_bytes(),
                                      self.financial_snapshot()), before)
            self.assertEqual(self.demo_mcp.calls, [])
            self.assert_response(self.client.get("/api/health"), 401)

    def test_setup_requires_same_origin_json_and_rejects_cross_site_without_side_effects(self):
        with self.setup_state() as store:
            before = database_dump(store.path), Path(store.path).read_bytes(), self.financial_snapshot()
            payload = {"username": self.username, "password": self.password}
            for headers in ({}, {"Origin": "http://attacker.example"}, {"Origin": "https://localhost"},
                            {"Origin": "http://localhost:9999"}, {"Origin": "http://localhost/"},
                            {"Origin": "null"}, {"Origin": "http://localhost.attacker.example"},
                            {"Origin": "http://localhost", "Sec-Fetch-Site": "cross-site"}):
                with self.subTest(headers=headers):
                    response = self.client.post("/api/auth/setup", json=payload, headers=headers)
                    self.assert_response(response, 403)
                    self.assertNotIn("set-cookie", response.headers)
                    self.assertEqual((database_dump(store.path), Path(store.path).read_bytes(),
                                      self.financial_snapshot()), before)
            for content_type in (None, "text/plain", "application/x-www-form-urlencoded", "multipart/form-data"):
                with self.subTest(content_type=content_type):
                    headers = {"Origin": "http://localhost"}
                    if content_type is not None:
                        headers["Content-Type"] = content_type
                    response = self.client.post("/api/auth/setup", content='{"username":"ignored"}', headers=headers)
                    self.assert_response(response, 415)
                    self.assertNotIn("set-cookie", response.headers)
            response = self.client.post("/api/auth/setup", data=payload, headers={"Origin": "http://localhost"})
            self.assert_response(response, 415)
            self.assertNotIn("set-cookie", response.headers)
            response = self.client.post("/api/auth/setup", content="{", headers={
                "Origin": "http://localhost", "Content-Type": "application/json",
            })
            self.assert_response(response, 422)
            self.assertNotIn("set-cookie", response.headers)
            self.assertFalse(self.client.cookies)
            self.assertFalse(store.configured())
            self.assertEqual((database_dump(store.path), Path(store.path).read_bytes(),
                              self.financial_snapshot()), before)
            self.assertEqual(self.demo_mcp.calls, [])

    def test_setup_rejects_nonlocal_https_peers_hosts_and_forwarded_spoofs(self):
        with self.setup_state() as store:
            before = database_dump(store.path), Path(store.path).read_bytes(), self.financial_snapshot()
            cases = [(origin, "192.0.2.15") for origin in (
                "https://finflow.example", "https://localhost", "https://127.0.0.1", "https://[::1]",
                "http://localhost", "http://127.0.0.1", "http://[::1]",
            )]
            cases.extend((origin, "127.0.0.1") for origin in (
                "https://finflow.example", "https://localhost.attacker.example", "http://testserver",
            ))
            cases.extend(("https://localhost", address) for address in (
                "localhost", "127.0.0.2", "::2", "::ffff:127.0.0.1",
            ))
            for origin, address in cases:
                with self.subTest(origin=origin, peer=address):
                    client = self.new_client(origin, peer=(address, 50_000))
                    response = client.post("/api/auth/setup", json={
                        "username": self.username, "password": self.password,
                    }, headers={
                        "Origin": origin, "X-Forwarded-For": "127.0.0.1", "X-Real-IP": "::1",
                        "X-Forwarded-Proto": "https", "X-Forwarded-Host": "localhost",
                        "Forwarded": "for=127.0.0.1;host=localhost;proto=https",
                    })
                    self.assert_response(response, 403)
                    self.assertNotIn("set-cookie", response.headers)
                    self.assertFalse(client.cookies)
                    self.assertFalse(store.configured())
                    self.assertEqual((database_dump(store.path), Path(store.path).read_bytes(),
                                      self.financial_snapshot()), before)
            self.assertEqual(self.demo_mcp.calls, [])

    def test_repeated_setup_preserves_existing_owner_and_sessions_even_authenticated(self):
        token, csrf = self.login()
        anonymous = self.new_client()
        before = database_dump(self.auth_path), Path(self.auth_path).read_bytes(), self.financial_snapshot()
        replacement_password = secrets.token_urlsafe(24)
        for client in (anonymous, self.client):
            for username, password in ((self.username, self.password), ("replacement-owner", replacement_password)):
                with self.subTest(authenticated=client is self.client, replacement=username != self.username):
                    response = client.post("/api/auth/setup", json={"username": username, "password": password},
                                           headers={"Origin": "http://localhost"})
                    self.assert_response(response, 409)
                    self.assertNotIn("set-cookie", response.headers)
                    self.assertEqual((database_dump(self.auth_path), Path(self.auth_path).read_bytes(),
                                      self.financial_snapshot()), before)
                    self.assertEqual(self.client.cookies.get(COOKIE_NAME), token)
                    self.assertEqual(self.main.auth_store.get_session(token)["csrf_token"], csrf)
        remote = self.new_client("https://localhost", peer=("192.0.2.15", 50_000))
        response = remote.post("/api/auth/setup", json={
            "username": "replacement-owner", "password": replacement_password,
        }, headers={"Origin": "https://localhost", "Cookie": f"{COOKIE_NAME}={token}",
                    "X-Forwarded-For": "127.0.0.1", "X-Forwarded-Proto": "https"})
        self.assert_response(response, 403)
        self.assertNotIn("set-cookie", response.headers)
        self.assertEqual((database_dump(self.auth_path), Path(self.auth_path).read_bytes(),
                          self.financial_snapshot()), before)
        self.assertFalse(anonymous.cookies)
        self.assertNotIn(replacement_password.encode(), Path(self.auth_path).read_bytes())
        self.assert_response(self.client.get("/api/health"), 200)
        for path in ("/api/auth/initialize", "/api/auth/register", "/api/auth/signup", "/api/auth/owner"):
            with self.subTest(banned_endpoint=path):
                response = self.client.post(path, json={
                    "username": "replacement-owner", "password": replacement_password,
                }, headers=self.unsafe_headers(csrf))
                self.assert_response(response, 404)
                self.assertNotIn("set-cookie", response.headers)
        self.assertEqual((database_dump(self.auth_path), Path(self.auth_path).read_bytes(),
                          self.financial_snapshot()), before)

    def test_concurrent_setup_has_one_winner_and_cannot_replace_enrolled_owner(self):
        with self.setup_state() as store:
            clients = [self.new_client(), self.new_client()]
            credentials = [{"username": f"temporary-racing-owner-{index}", "password": secrets.token_urlsafe(24)}
                           for index in range(2)]
            before = self.financial_snapshot()
            start = Barrier(2)

            def enroll(index):
                start.wait(timeout=10)
                return clients[index].post("/api/auth/setup", json=credentials[index],
                                           headers={"Origin": "http://localhost"})

            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(enroll, index) for index in range(2)]
                responses = [future.result(timeout=60) for future in futures]
            self.assertEqual(sorted(response.status_code for response in responses), [200, 409],
                             [response.text for response in responses])
            winner = next(index for index, response in enumerate(responses) if response.status_code == 200)
            loser = 1 - winner
            for response in responses:
                self.assert_response(response, response.status_code)
            self.assertEqual(responses[winner].json(), {"authenticated": True})
            self.assertNotIn("set-cookie", responses[loser].headers)
            self.assertFalse(clients[loser].cookies)
            token = clients[winner].cookies.get(COOKIE_NAME)
            self.assertTrue(token)
            with store.connect() as connection:
                owners = [dict(row) for row in connection.execute("SELECT * FROM owner")]
                sessions = [dict(row) for row in connection.execute("SELECT * FROM sessions")]
            self.assertEqual(len(owners), 1)
            self.assertEqual(owners[0]["username"], credentials[winner]["username"])
            self.assertTrue(check_password(credentials[winner]["password"], owners[0]["password_hash"]))
            self.assertFalse(check_password(credentials[loser]["password"], owners[0]["password_hash"]))
            self.assertEqual((owners[0]["failed_attempts"], owners[0]["locked_until"]), (0, 0))
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0]["token_hash"], hashlib.sha256(token.encode()).hexdigest())
            session = self.assert_response(clients[winner].get("/api/auth/session"), 200).json()
            self.assertEqual(session["username"], credentials[winner]["username"])
            self.assertEqual(session["csrf_token"], sessions[0]["csrf_token"])
            self.assertEqual(self.assert_response(clients[loser].get("/api/auth/session"), 200).json(), {
                "authenticated": False, "configured": True,
            })
            self.assert_response(clients[winner].get("/api/health"), 200)
            self.assert_response(clients[loser].get("/api/health"), 401)
            enrolled = database_dump(store.path), Path(store.path).read_bytes()
            for client in clients:
                response = client.post("/api/auth/setup", json=credentials[loser], headers={"Origin": "http://localhost"})
                self.assert_response(response, 409)
                self.assertNotIn("set-cookie", response.headers)
                self.assertEqual((database_dump(store.path), Path(store.path).read_bytes()), enrolled)
            self.assertEqual(clients[winner].cookies.get(COOKIE_NAME), token)
            self.assertEqual(store.get_session(token)["csrf_token"], session["csrf_token"])
            self.assertEqual(self.financial_snapshot(), before)
            self.assertEqual(self.demo_mcp.calls, [])

    def test_default_credentials_and_forged_session_cannot_bypass_owner(self):
        for username, password in (("admin", "admin"), ("owner", "password"), ("demo", "demo")):
            response = self.client.post("/api/auth/login", json={"username": username, "password": password},
                                        headers={"Origin": "http://localhost"})
            self.assert_response(response, 401)
            self.assertNotIn("set-cookie", response.headers)
        for token in ("admin", "true", "x" * 129):
            with self.subTest(token_length=len(token)):
                response = self.client.get("/api/health", headers={"Cookie": f"{COOKIE_NAME}={token}"})
                self.assert_response(response, 401)
        response = self.assert_response(self.client.get("/api/auth/session?authenticated=true&username=admin"), 200)
        self.assertEqual(response.json(), {"authenticated": False, "configured": True})
        self.assertFalse(self.client.cookies)

    def test_wrong_and_unknown_username_have_generic_login_responses(self):
        errors = []
        for username, password in ((self.username, "wrong-password"), ("unknown-user", self.password)):
            response = self.client.post("/api/auth/login", json={"username": username, "password": password},
                                        headers={"Origin": "http://localhost"})
            self.assert_response(response, 401)
            errors.append(response.json())
            self.assertNotIn("set-cookie", response.headers)
            self.assertNotIn(self.username, response.text)
            self.assertNotIn(self.password, response.text)
        self.assertEqual(errors, [{"detail": "Incorrect username or password."}] * 2)
        self.assertFalse(self.client.cookies)
        with self.main.auth_store.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)

    def test_login_validation_does_not_create_sessions_or_change_failure_counter(self):
        before = database_dump(self.auth_path)
        for payload in (
            {}, {"username": "", "password": self.password},
            {"username": "u" * 81, "password": self.password},
            {"username": self.username, "password": ""},
            {"username": self.username, "password": "p" * 129},
        ):
            with self.subTest(payload_lengths={key: len(value) for key, value in payload.items()}):
                response = self.client.post("/api/auth/login", json=payload, headers={"Origin": "http://localhost"})
                self.assert_response(response, 422)
                self.assertNotIn("set-cookie", response.headers)
                self.assertEqual(database_dump(self.auth_path), before)

    def test_login_requires_same_origin_and_json_but_not_csrf(self):
        before = database_dump(self.auth_path)
        payload = {"username": self.username, "password": self.password}
        for headers in ({}, {"Origin": "http://attacker.example"}, {"Origin": "https://localhost"},
                        {"Origin": "null"}, {"Origin": "http://localhost", "Sec-Fetch-Site": "cross-site"}):
            with self.subTest(headers=headers):
                self.assert_response(self.client.post("/api/auth/login", json=payload, headers=headers), 403)
                self.assertEqual(database_dump(self.auth_path), before)
        self.assert_response(self.client.post("/api/auth/login", content='{"username":"ignored"}', headers={
            "Origin": "http://localhost", "Content-Type": "text/plain",
        }), 415)
        self.assert_response(self.client.post("/api/auth/login", data=payload, headers={"Origin": "http://localhost"}), 415)
        self.assertEqual(database_dump(self.auth_path), before)
        self.login()
        self.assert_response(self.client.get("/login"), 303)
        self.assertEqual(self.client.get("/login").headers["location"], "/")

    def test_http_rate_limit_blocks_correct_password_and_expires_without_sleep(self):
        with patch("app.auth.time.time", return_value=10_000.0) as clock:
            for _ in range(5):
                response = self.client.post("/api/auth/login", json={"username": self.username, "password": "wrong"},
                                            headers={"Origin": "http://localhost"})
                self.assert_response(response, 401)
            clock.return_value = 10_059.999
            response = self.client.post("/api/auth/login", json={"username": self.username, "password": self.password},
                                        headers={"Origin": "http://localhost"})
            self.assert_response(response, 429)
            self.assertEqual(response.headers["retry-after"], "60")
            self.assertNotIn("set-cookie", response.headers)
            clock.return_value = 10_060.0
            self.login()
            self.assert_response(self.client.get("/api/health"), 200)
            with self.main.auth_store.connect() as connection:
                row = connection.execute("SELECT failed_attempts, locked_until FROM owner").fetchone()
            self.assertEqual(tuple(row), (0, 0))

    def test_all_unsafe_methods_reject_missing_or_mismatched_origin(self):
        _, csrf = self.login()
        before = self.financial_snapshot(), database_dump(self.auth_path)
        bad_headers = [
            {}, {"Origin": "http://other.example"}, {"Origin": "https://localhost"},
            {"Origin": "http://localhost:9999"}, {"Origin": "http://localhost/"},
            {"Origin": "http://localhost.attacker.example"}, {"Origin": "null"},
            {"Origin": "http://localhost", "Sec-Fetch-Site": "cross-site"},
        ]
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            for headers in bad_headers:
                with self.subTest(method=method, headers=headers):
                    response = self.client.request(method, "/api/runs", json={"ticker": "DEMO"},
                                                   headers={"X-CSRF-Token": csrf, **headers})
                    self.assert_response(response, 403)
        self.assertEqual((self.financial_snapshot(), database_dump(self.auth_path)), before)

    def test_every_mutating_api_rejects_missing_or_wrong_csrf(self):
        _, csrf = self.login()
        before = self.financial_snapshot(), database_dump(self.auth_path)
        for path, payload in (
            ("/api/runs", {"ticker": "DEMO"}),
            ("/api/runs/no-run/reviews/no-review", {"decision": "approve"}),
            ("/api/assistant", {"message": "Show revenue"}), ("/api/auth/logout", {}),
        ):
            for supplied in (None, "wrong-csrf", csrf + "x"):
                with self.subTest(path=path, csrf=supplied is not None):
                    headers = {"Origin": "http://localhost"}
                    if supplied is not None:
                        headers["X-CSRF-Token"] = supplied
                    self.assert_response(self.client.post(path, json=payload, headers=headers), 403)
        self.assertEqual((self.financial_snapshot(), database_dump(self.auth_path)), before)
        other = self.new_client()
        _, other_csrf = self.login(other)
        self.assertNotEqual(other_csrf, csrf)
        self.assert_response(other.post("/api/auth/logout", json={}, headers=self.unsafe_headers(csrf, other)), 403)

    def test_all_unsafe_methods_reject_text_plain_forms_and_missing_content_type(self):
        _, csrf = self.login()
        before = self.financial_snapshot(), database_dump(self.auth_path)
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            for content_type in (None, "text/plain", "application/x-www-form-urlencoded", "multipart/form-data"):
                with self.subTest(method=method, content_type=content_type):
                    headers = self.unsafe_headers(csrf)
                    if content_type is not None:
                        headers["Content-Type"] = content_type
                    response = self.client.request(method, "/api/runs", content='{"ticker":"DEMO"}', headers=headers)
                    self.assert_response(response, 415)
        self.assertEqual((self.financial_snapshot(), database_dump(self.auth_path)), before)
        self.assert_response(self.client.post("/api/assistant", content='{"message":"Help"}', headers={
            **self.unsafe_headers(csrf), "Content-Type": "application/json; charset=utf-8",
        }), 200)

    def test_localhost_http_is_allowed_for_all_loopback_hosts(self):
        for origin, address in (("http://localhost", "127.0.0.1"), ("http://127.0.0.1", "127.0.0.1"),
                                ("http://[::1]", "::1")):
            with self.subTest(origin=origin):
                client = self.new_client(origin, peer=(address, 50_000))
                self.assert_response(client.get("/login"), 200)
                self.login(client)
                self.assert_response(client.get("/api/health"), 200)

    def test_remote_peer_cannot_spoof_localhost_or_forwarded_loopback(self):
        token, _ = self.login()
        for origin in ("http://localhost", "http://127.0.0.1", "http://[::1]"):
            with self.subTest(origin=origin):
                client = self.new_client(origin, peer=("192.0.2.15", 50_000))
                response = client.get("/api/health", headers={
                    "Cookie": f"{COOKIE_NAME}={token}", "X-Forwarded-For": "127.0.0.1",
                    "X-Forwarded-Proto": "https", "X-Real-IP": "127.0.0.1",
                })
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json(), {"detail": "HTTPS is required outside localhost."})

    def test_remote_http_is_forbidden_even_with_valid_cookie_or_forwarded_https(self):
        token, _ = self.login()
        for origin in ("http://finflow.example", "http://192.0.2.10", "http://testserver"):
            client = self.new_client(origin)
            for path in ("/login", "/static/login.css", "/api/auth/session", "/api/health", "/openapi.json"):
                with self.subTest(origin=origin, path=path):
                    response = client.get(path, headers={
                        "Cookie": f"{COOKIE_NAME}={token}", "X-Forwarded-Proto": "https",
                        "X-Forwarded-Host": "localhost",
                    })
                    self.assertEqual(response.status_code, 403)
                    self.assertEqual(response.json(), {"detail": "HTTPS is required outside localhost."})
            response = client.post("/api/auth/login", json={"username": self.username, "password": self.password},
                                   headers={"Origin": origin})
            self.assertEqual(response.status_code, 403)
            self.assertNotIn("set-cookie", response.headers)

    def test_https_login_cookie_is_secure_httponly_strict_and_eight_hours(self):
        client = self.new_client("https://finflow.example")
        response = client.post("/api/auth/login", json={"username": self.username, "password": self.password},
                               headers={"Origin": "https://finflow.example"})
        self.assert_response(response, 200)
        cookie = SimpleCookie()
        cookie.load(response.headers["set-cookie"])
        morsel = cookie[COOKIE_NAME]
        self.assertTrue(morsel["secure"])
        self.assertTrue(morsel["httponly"])
        self.assertEqual(morsel["samesite"].lower(), "strict")
        self.assertEqual(morsel["max-age"], "28800")
        self.assertEqual(morsel["path"], "/")
        self.assertFalse(morsel["domain"])
        self.assert_response(client.get("/api/health"), 200)
        session = self.assert_response(client.get("/api/auth/session"), 200).json()
        response = client.post("/api/auth/logout", json={}, headers=self.unsafe_headers(session["csrf_token"], client))
        self.assert_response(response, 200)
        cookie.load(response.headers["set-cookie"])
        self.assertEqual(cookie[COOKIE_NAME]["max-age"], "0")
        self.assertTrue(cookie[COOKIE_NAME]["secure"])
        self.assertTrue(cookie[COOKIE_NAME]["httponly"])
        self.assertEqual(cookie[COOKIE_NAME]["samesite"].lower(), "strict")

    def test_local_http_cookie_attributes_and_secure_cookie_is_not_sent_over_http(self):
        token, _ = self.login()
        self.assertIsNotNone(self.main.auth_store.get_session(token))
        for cookie in self.client.cookies.jar:
            if cookie.name == COOKIE_NAME:
                self.assertFalse(cookie.secure)
                self.assertTrue(cookie.has_nonstandard_attr("HttpOnly"))
                self.assertEqual(cookie.get_nonstandard_attr("SameSite"), "strict")
        secure_client = self.new_client("https://localhost")
        self.login(secure_client)
        self.assertEqual(secure_client.get("http://localhost/api/auth/session").json(), {
            "authenticated": False, "configured": True,
        })

    def test_logout_revokes_old_token_and_old_token_replay_fails(self):
        token, csrf = self.login()
        response = self.client.post("/api/auth/logout", json={}, headers=self.unsafe_headers(csrf))
        self.assert_response(response, 200)
        self.assertEqual(response.json(), {"authenticated": False})
        self.assertIsNone(self.main.auth_store.get_session(token))
        self.assertIsNone(self.client.cookies.get(COOKIE_NAME))
        self.assert_response(self.client.get("/api/health"), 401)
        replay = self.new_client()
        headers = {"Cookie": f"{COOKIE_NAME}={token}"}
        self.assert_response(replay.get("/api/health", headers=headers), 401)
        self.assert_response(replay.get("/", headers=headers), 303)
        self.assertEqual(replay.get("/api/auth/session", headers=headers).json(), {
            "authenticated": False, "configured": True,
        })
        self.assert_response(replay.post("/api/auth/logout", json={}, headers={**headers, **self.unsafe_headers(csrf)}), 401)
        new_token, new_csrf = self.login()
        self.assertNotEqual(new_token, token)
        self.assertNotEqual(new_csrf, csrf)

    def test_replacement_login_invalidates_previous_token_and_csrf_only(self):
        token, csrf = self.login()
        independent = self.new_client()
        independent_token, _ = self.login(independent)
        replacement, replacement_csrf = self.login()
        self.assertNotEqual(replacement, token)
        self.assertNotEqual(replacement_csrf, csrf)
        self.assertIsNone(self.main.auth_store.get_session(token))
        self.assertIsNotNone(self.main.auth_store.get_session(replacement))
        self.assertIsNotNone(self.main.auth_store.get_session(independent_token))
        replay = self.new_client()
        self.assert_response(replay.get("/api/health", headers={"Cookie": f"{COOKIE_NAME}={token}"}), 401)
        self.assert_response(self.client.post("/api/auth/logout", json={}, headers=self.unsafe_headers(csrf)), 403)
        self.assert_response(self.client.get("/api/health"), 200)
        self.assert_response(self.client.post("/api/auth/logout", json={}, headers=self.unsafe_headers(replacement_csrf)), 200)
        self.assert_response(independent.get("/api/health"), 200)

    def test_failed_replacement_login_leaves_existing_session_valid(self):
        token, csrf = self.login()
        response = self.client.post("/api/auth/login", json={"username": self.username, "password": "wrong"},
                                   headers={"Origin": "http://localhost"})
        self.assert_response(response, 401)
        self.assertNotIn("set-cookie", response.headers)
        self.assertEqual(self.client.cookies.get(COOKIE_NAME), token)
        self.assertEqual(self.main.auth_store.get_session(token)["csrf_token"], csrf)
        self.assert_response(self.client.get("/api/health"), 200)

    def test_expired_cookie_is_not_authenticated_and_fresh_login_replaces_it(self):
        with patch("app.auth.time.time", return_value=20_000.0) as clock:
            token, csrf = self.login()
            clock.return_value = 20_000.0 + 28_800 - 0.001
            self.assert_response(self.client.get("/api/health"), 200)
            clock.return_value += 0.001
            self.assert_response(self.client.get("/api/health"), 401)
            self.assert_response(self.client.get("/static/index.html"), 303)
            self.assert_response(self.client.get("/docs"), 303)
            self.assert_response(self.client.get("/login"), 200)
            self.assertEqual(self.assert_response(self.client.get("/api/auth/session"), 200).json(), {
                "authenticated": False, "configured": True,
            })
            self.assert_response(self.client.post("/api/auth/logout", json={}, headers=self.unsafe_headers(csrf)), 401)
            replacement, new_csrf = self.login()
            self.assertNotEqual(replacement, token)
            self.assertNotEqual(new_csrf, csrf)
            self.assertIsNone(self.main.auth_store.get_session(token))
            with self.main.auth_store.connect() as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 1)

    def test_reopened_store_accepts_persisted_cookie_without_restarting_workflow(self):
        token, csrf = self.login()
        reopened = AuthStore(self.auth_path)
        self.assertEqual(reopened.get_session(token)["csrf_token"], csrf)
        from app.auth import AuthMiddleware
        middleware = self.main.app.middleware_stack
        while not isinstance(middleware, AuthMiddleware):
            middleware = middleware.app
        with patch.object(middleware, "store", reopened), patch.object(self.main, "auth_store", reopened):
            self.assert_response(self.client.get("/api/health"), 200)
            self.assertEqual(self.client.get("/api/auth/session").json()["csrf_token"], csrf)
            self.assert_response(self.client.post("/api/auth/logout", json={}, headers=self.unsafe_headers(csrf)), 200)
        self.assertIsNone(AuthStore(self.auth_path).get_session(token))

    def test_no_store_and_security_headers_cover_public_denied_and_authenticated_responses(self):
        responses = [self.client.get("/login"), self.client.get("/api/auth/session"),
                     self.client.get("/api/health"), self.client.get("/static/index.html"),
                     self.client.post("/api/auth/login", json={})]
        _, csrf = self.login()
        responses.extend([
            self.client.get("/api/health"), self.client.get("/api/runs/missing"),
            self.client.post("/api/assistant", json={}, headers=self.unsafe_headers(csrf)),
            self.client.post("/api/assistant", content="plain", headers=self.unsafe_headers(csrf)),
            self.client.put("/api/runs", json={}, headers=self.unsafe_headers(csrf)),
        ])
        self.assertEqual({response.status_code for response in responses}, {200, 303, 401, 403, 404, 405, 415, 422})
        for response in responses:
            with self.subTest(status=response.status_code, url=str(response.request.url)):
                self.assertEqual(response.headers.get("cache-control"), "no-store")
                self.assertEqual(response.headers.get("x-content-type-options"), "nosniff")
                self.assertEqual(response.headers.get("x-frame-options"), "DENY")
                self.assertEqual(response.headers.get("referrer-policy"), "same-origin")

    def test_remote_http_rejection_is_also_no_store(self):
        client = self.new_client("http://finflow.example")
        for path in ("/login", "/api/auth/session", "/api/auth/login"):
            with self.subTest(path=path):
                response = client.post(path, json={}) if path.endswith("/login") and path.startswith("/api/") else client.get(path)
                self.assert_response(response, 403)

    def test_authenticated_demo_audit_review_and_chat_preserve_financial_results(self):
        _, csrf = self.login()
        run = self.create_demo(csrf)
        explanations = {metric["key"]: metric["explanation"] for metric in run["state"]["metrics"]}
        completed = self.complete_demo(run, csrf)
        metrics = {metric["key"]: metric for metric in completed["state"]["metrics"]}
        self.assertEqual(metrics["revenue"]["value"], 100_000_000)
        self.assertEqual(metrics["total_liabilities"]["value"], 30_000_000)
        self.assertEqual(metrics["total_liabilities"]["original_value"], 35_000_000)
        self.assertEqual(metrics["total_liabilities"]["status"], "corrected")
        self.assertEqual({key: metric["explanation"] for key, metric in metrics.items()}, explanations)
        self.assertEqual([event["selected_tool"] for event in completed["state"]["agent"]["tool_trace"]].count("explain_metrics"), 1)
        before = self.financial_snapshot()
        response = self.client.post("/api/assistant", json={
            "message": "What is total liabilities now?", "run_id": run["id"],
            "history": [{"role": "user", "content": "Which value changed?"},
                        {"role": "assistant", "content": "Total Liabilities changed."}],
        }, headers=self.unsafe_headers(csrf))
        answer = self.assert_response(response, 200).json()
        self.assertEqual(answer["generated_by"], "rule_based_fallback")
        self.assertIn("30,000,000 USD", answer["answer"])
        self.assertIn("35,000,000 USD", answer["answer"])
        self.assertIn("corrected", answer["answer"])
        duplicate = self.client.post(f"/api/runs/{run['id']}/reviews/{run['reviews'][0]['id']}",
                                     json={"decision": "approve"}, headers=self.unsafe_headers(csrf))
        self.assert_response(duplicate, 409)
        self.assertEqual(self.financial_snapshot(), before)

    def test_authenticated_pdf_docx_exports_and_reads_are_immutable(self):
        _, csrf = self.login()
        run = self.create_demo(csrf)
        for complete in (False, True):
            if complete:
                run = self.complete_demo(run, csrf)
            before = self.financial_snapshot()
            for format in ("pdf", "docx"):
                with self.subTest(completed=complete, format=format):
                    url = f"/api/runs/{run['id']}/report?format={format}"
                    anonymous = self.new_client()
                    self.assert_response(anonymous.get(url), 401)
                    response = self.assert_response(self.client.get(url), 200)
                    self.assertIn(f"FinFlow_DEMO_{run['id'][:8]}.{format}", response.headers["content-disposition"])
                    self.assertIn("attachment;", response.headers["content-disposition"])
                    if format == "pdf":
                        self.assertEqual(response.headers["content-type"], "application/pdf")
                        self.assertTrue(response.content.startswith(b"%PDF-"))
                        document = PdfReader(BytesIO(response.content))
                        text = "\n".join(page.extract_text() or "" for page in document.pages)
                    else:
                        self.assertEqual(response.headers["content-type"], "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
                        self.assertTrue(response.content.startswith(b"PK"))
                        document = Document(BytesIO(response.content))
                        text = "\n".join([
                            *(paragraph.text for paragraph in document.paragraphs),
                            *(cell.text for table in document.tables for row in table.rows for cell in row.cells),
                        ])
                    text = " ".join(text.split())
                    self.assertIn("Demo Manufacturing Corp.", text)
                    self.assertIn(run["id"], text)
                    self.assertIn("Completed audit snapshot" if complete else "PROVISIONAL AUDIT SNAPSHOT", text)
                    for metric in run["state"]["metrics"]:
                        self.assertIn(metric["label"], text)
                        self.assertIn(" ".join(metric["explanation"]["extraction_reason"].split()), text)
                    self.assertEqual(self.financial_snapshot(), before)
            for path in (
                "/", "/static/index.html", "/static/app.js", "/static/styles.css",
                "/docs", "/redoc", "/openapi.json", "/api/health", "/api/companies", "/api/mcp/tools",
                "/api/runs", f"/api/runs/{run['id']}", f"/api/runs/{run['id']}/audit/verify",
            ):
                with self.subTest(path=path):
                    self.assert_response(self.client.get(path), 200)
            self.assertEqual(self.client.get(f"/api/runs/{run['id']}").json(), run)
            self.assert_response(self.client.get(f"/api/runs/{run['id']}/report?format=txt"), 422)
            self.assert_response(self.client.get("/api/runs/missing/report?format=pdf"), 404)
            self.assertEqual(self.financial_snapshot(), before)

    def test_temporary_demo_audit_log_remains_append_only_and_hash_verified(self):
        _, csrf = self.login()
        run = self.complete_demo(self.create_demo(csrf), csrf)
        before = self.financial_snapshot()
        for statement in (
            "UPDATE audit_log SET decision='reject' WHERE run_id=?",
            "DELETE FROM audit_log WHERE run_id=?",
        ):
            with self.subTest(statement=statement):
                with self.main.database.connect() as connection:
                    with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                        connection.execute(statement, (run["id"],))
                self.assertEqual(self.financial_snapshot(), before)
        verification = self.assert_response(self.client.get(f"/api/runs/{run['id']}/audit/verify"), 200).json()
        self.assertTrue(verification["valid"])
        self.assertEqual(verification["entries_checked"], 4)
        self.assertEqual(self.financial_snapshot(), before)


if __name__ == "__main__":
    unittest.main()
