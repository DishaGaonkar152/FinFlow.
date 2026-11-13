import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    ticker TEXT NOT NULL,
                    company_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS checkpoints (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    stage TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(run_id, stage)
                );
                CREATE TABLE IF NOT EXISTS reviews (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    metric_key TEXT NOT NULL,
                    label TEXT NOT NULL,
                    original_value REAL,
                    confidence REAL NOT NULL,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    corrected_value REAL,
                    reviewer TEXT,
                    note TEXT,
                    created_at TEXT NOT NULL,
                    decided_at TEXT,
                    UNIQUE(run_id, metric_key)
                );
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    review_id TEXT NOT NULL REFERENCES reviews(id),
                    metric_key TEXT NOT NULL,
                    original_value REAL,
                    corrected_value REAL,
                    decision TEXT NOT NULL,
                    reviewer TEXT NOT NULL,
                    note TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE
                );
                CREATE TRIGGER IF NOT EXISTS audit_log_no_update
                BEFORE UPDATE ON audit_log
                BEGIN SELECT RAISE(ABORT, 'Audit log is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
                BEFORE DELETE ON audit_log
                BEGIN SELECT RAISE(ABORT, 'Audit log is append-only'); END;
                """
            )

    def create_run(self, run_id: str, ticker: str, company_name: str, state: dict) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO runs VALUES (?, ?, ?, 'processing', 'created', ?, NULL, ?, ?)",
                (run_id, ticker, company_name, json.dumps(state), now, now),
            )

    def save_run(self, run_id: str, status: str, stage: str, state: dict, error: str | None = None) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE runs SET status=?, stage=?, state_json=?, error=?, updated_at=? WHERE id=?",
                (status, stage, json.dumps(state), error, utc_now(), run_id),
            )

    def checkpoint(self, run_id: str, stage: str, state: dict) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO checkpoints(run_id, stage, state_json, created_at) VALUES (?, ?, ?, ?)
                   ON CONFLICT(run_id, stage) DO UPDATE SET state_json=excluded.state_json""",
                (run_id, stage, json.dumps(state), utc_now()),
            )
            connection.execute(
                "UPDATE runs SET stage=?, state_json=?, updated_at=? WHERE id=?",
                (stage, json.dumps(state), utc_now(), run_id),
            )

    def get_run(self, run_id: str) -> dict | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["state"] = json.loads(result.pop("state_json"))
        return result

    def list_runs(self) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT r.id, r.ticker, r.company_name, r.status, r.stage, r.error,
                          r.created_at, r.updated_at,
                          COUNT(CASE WHEN v.status='pending' THEN 1 END) AS pending_reviews,
                          COUNT(CASE WHEN v.status!='pending' THEN 1 END) AS completed_reviews
                   FROM runs r LEFT JOIN reviews v ON v.run_id=r.id
                   GROUP BY r.id ORDER BY r.created_at DESC"""
            ).fetchall()
        return [dict(row) for row in rows]

    def create_reviews(self, run_id: str, metrics: list[dict]) -> None:
        import uuid

        with self.connect() as connection:
            for metric in metrics:
                if metric["status"] != "pending":
                    continue
                reasons = [v["message"] for v in metric["validations"] if v["status"] == "failed"]
                if metric["confidence_reasons"]:
                    reasons.extend(metric["confidence_reasons"])
                connection.execute(
                    """INSERT OR IGNORE INTO reviews(
                           id, run_id, metric_key, label, original_value, confidence, reason, created_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        str(uuid.uuid4()), run_id, metric["key"], metric["label"], metric["value"],
                        metric["confidence"], " | ".join(reasons) or "Manual review required", utc_now(),
                    ),
                )

    def get_reviews(self, run_id: str) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM reviews WHERE run_id=? ORDER BY created_at, metric_key", (run_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def get_audit(self, run_id: str) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM audit_log WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def audit_hash(entry: dict) -> str:
        payload = {k: v for k, v in entry.items() if k not in {"id", "entry_hash"}}
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def verify_audit(self, run_id: str) -> dict:
        entries = self.get_audit(run_id)
        previous_hash = "0" * 64
        for entry in entries:
            if entry["previous_hash"] != previous_hash or self.audit_hash(entry) != entry["entry_hash"]:
                return {"valid": False, "entries_checked": entry["id"], "run_id": run_id}
            previous_hash = entry["entry_hash"]
        return {"valid": True, "entries_checked": len(entries), "run_id": run_id}
