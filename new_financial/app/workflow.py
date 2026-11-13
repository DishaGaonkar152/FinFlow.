import os
import uuid
from datetime import datetime, timezone

from .agent import FinancialAuditorAgent
from .assistant import DashboardAssistant
from .db import Database, utc_now
from .explanations import ExplanationGenerator
from .extractor import METRIC_SPECS
from .mcp_client import EdgarMCPClient
from .sec import SecClient


class AuditWorkflow:
    def __init__(self, database: Database, sec_client: SecClient | None = None, checkpoint_path: str | None = None, mcp_client=None, explanation_generator=None, dashboard_assistant=None):
        self.db = database
        self.sec = sec_client or SecClient()
        self.mcp = mcp_client or EdgarMCPClient()
        self.explainer = explanation_generator or ExplanationGenerator()
        self.assistant = dashboard_assistant or DashboardAssistant()
        checkpoint_path = checkpoint_path or os.getenv("AUDITOR_AGENT_DB_PATH", f"{database.path}.langgraph")
        self.agent = FinancialAuditorAgent(self, checkpoint_path)

    def start(self, ticker: str, confidence_threshold: float = 0.85, variance_threshold: float = 0.50) -> dict:
        company = self.sec.company(ticker)
        identifier = "DEMO" if company["ticker"] == "DEMO" else company["cik"]
        if identifier != "DEMO":
            company = self.sec.company_details(identifier)
            if not company["filing_available"]:
                raise ValueError(f"No Form 10-K is available for {company['name']} (CIK {company['cik']}). A 20-F or 40-F is not a 10-K.")
        run_id = str(uuid.uuid4())
        state = {
            "company": company,
            "filing": None,
            "metrics": [],
            "confidence_threshold": confidence_threshold,
            "variance_threshold": variance_threshold,
            "report": None,
            "agent": None,
        }
        self.db.create_run(run_id, company["ticker"], company["name"], state)
        try:
            result = self.agent.start(run_id, identifier, confidence_threshold, variance_threshold)
            self._sync_agent_result(run_id, result)
        except Exception as error:
            run = self.db.get_run(run_id)
            self.db.save_run(run_id, "failed", run["stage"], run["state"], str(error))
        return self.db.get_run(run_id)

    def _sync_agent_result(self, run_id: str, result: dict) -> None:
        run = self.db.get_run(run_id)
        state = run["state"]
        filing = result.get("filing")
        if filing:
            state["company"] = filing["company"]
            state["filing"] = {
                "accession": filing["accession"],
                "report_date": filing["report_date"],
                "url": filing["url"],
                "offline": filing["offline"],
            }
        state["metrics"] = result.get("metrics", [])
        state["report"] = result.get("report")
        state["agent"] = {
            "framework": "LangGraph",
            "checkpoint_store": "SQLite",
            "tool_selection": "dynamic_policy",
            "mcp_transport": "stdio",
            "mcp_tools": ["retrieve_10k_filing", "lookup_xbrl_company_facts"],
            "thread_id": run_id,
            "next_action": result.get("next_action"),
            "available_tools": [tool.name for tool in self.agent.tools],
            "tool_trace": result.get("tool_trace", []),
            "explanations": {
                "llm_configured": self.explainer.configured,
                "provider": "openai_compatible" if self.explainer.configured else "deterministic_rules",
                "model": self.explainer.model if self.explainer.configured else None,
            },
        }
        if result.get("status") == "completed":
            status = "completed"
            stage = "complete"
        elif any(metric["status"] == "pending" for metric in state["metrics"]):
            if not self.db.get_reviews(run_id):
                self.db.create_reviews(run_id, state["metrics"])
            status = "awaiting_review"
            stage = "awaiting_human_review"
        else:
            status = "processing"
            stage = result.get("next_action", "agent_processing")
        self.db.checkpoint(run_id, stage, state)
        self.db.save_run(run_id, status, stage, state)

    @staticmethod
    def _add_validation(metrics: list[dict], keys: set[str], rule: str, status: str, message: str, details: dict | None = None) -> None:
        for metric in metrics:
            if metric["key"] in keys:
                metric.setdefault("validations", []).append({
                    "rule": rule, "status": status, "message": message, "details": details or {},
                })

    def _validate(self, metrics: list[dict], segment_data: dict | None, variance_threshold: float) -> None:
        by_key = {metric["key"]: metric for metric in metrics}
        for metric in metrics:
            metric["validations"] = []
            if metric["value"] is None:
                metric["validations"].append({
                    "rule": "extraction", "status": "failed",
                    "message": "No usable value was extracted; reviewer input is required.", "details": {},
                })

        accounting_keys = {"total_assets", "total_liabilities", "shareholders_equity"}
        if all(by_key[key]["value"] is not None for key in accounting_keys):
            assets = by_key["total_assets"]["value"]
            liabilities = by_key["total_liabilities"]["value"]
            equity = by_key["shareholders_equity"]["value"]
            difference = assets - liabilities - equity
            tolerance = max(1000, abs(assets) * 0.001)
            passed = abs(difference) <= tolerance
            self._add_validation(
                metrics, accounting_keys, "accounting_equation", "passed" if passed else "failed",
                f"Assets − liabilities − equity = {difference:,.2f} (tolerance {tolerance:,.2f}).",
                {"difference": difference, "tolerance": tolerance},
            )
        else:
            self._add_validation(metrics, accounting_keys, "accounting_equation", "skipped", "Accounting equation requires all three values.")

        revenue = by_key["revenue"]
        if segment_data and revenue["value"] is not None:
            difference = segment_data["total"] - revenue["value"]
            tolerance = max(1000, abs(revenue["value"]) * 0.001)
            passed = abs(difference) <= tolerance
            self._add_validation(
                metrics, {"revenue"}, "segment_total", "passed" if passed else "failed",
                f"Segment total differs from consolidated revenue by {difference:,.2f}.",
                {"segment_total": segment_data["total"], "difference": difference, "members": segment_data["members"]},
            )
        else:
            self._add_validation(metrics, {"revenue"}, "segment_total", "skipped", "No comparable segment breakdown was available.")

        for metric in metrics:
            current = metric["value"]
            previous = metric["previous_value"]
            if current is None or previous is None:
                metric["validations"].append({
                    "rule": "year_on_year", "status": "skipped",
                    "message": "A comparable prior-year value was not available.", "details": {},
                })
                continue
            change = None if previous == 0 else (current - previous) / abs(previous)
            unusual = change is None or abs(change) > variance_threshold
            metric["validations"].append({
                "rule": "year_on_year",
                "status": "failed" if unusual else "passed",
                "message": "Prior-year value is zero; change requires review." if change is None else f"Year-on-year change is {change:.1%}; threshold is {variance_threshold:.1%}.",
                "details": {"change_ratio": change, "threshold": variance_threshold},
            })

    @staticmethod
    def _ground_truth(company_facts: dict, metrics: list[dict], accession: str) -> None:
        facts_by_tag = company_facts.get("facts", {}).get("us-gaap", {})
        for metric, spec in zip(metrics, METRIC_SPECS):
            candidates = []
            for tag in spec.tags:
                tag_data = facts_by_tag.get(tag, {})
                for value in tag_data.get("units", {}).get(spec.unit, []):
                    if value.get("form") != "10-K" or value.get("end") != metric["end_date"]:
                        continue
                    if spec.duration and value.get("start") != metric["start_date"]:
                        continue
                    candidates.append((tag, value))
            if not candidates:
                metric["ground_truth"] = None
                continue
            candidates.sort(key=lambda item: (item[1].get("accn") == accession, item[1].get("filed", "")), reverse=True)
            tag, value = candidates[0]
            metric["ground_truth"] = {
                "value": value["val"], "tag": f"us-gaap:{tag}", "accn": value.get("accn"),
                "filed": value.get("filed"), "start": value.get("start"), "end": value.get("end"),
            }

    def submit_decision(self, run_id: str, review_id: str, request) -> dict:
        run = self.db.get_run(run_id)
        if not run:
            raise LookupError("Run not found")
        if run["status"] != "awaiting_review":
            raise ValueError("Run is not awaiting human review")

        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            review = connection.execute(
                "SELECT * FROM reviews WHERE id=? AND run_id=?", (review_id, run_id)
            ).fetchone()
            if not review:
                raise LookupError("Review not found")
            if review["status"] != "pending":
                raise ValueError("This review has already been decided")
            if request.decision == "correct" and request.corrected_value is None:
                raise ValueError("A corrected value is required")
            if request.decision == "approve" and review["original_value"] is None:
                raise ValueError("A missing value cannot be approved; correct or reject it")

            corrected_value = float(request.corrected_value) if request.decision == "correct" else None
            connection.execute(
                """UPDATE reviews SET status=?, corrected_value=?, reviewer=?, note=?, decided_at=?
                   WHERE id=?""",
                (request.decision + "d" if request.decision == "approve" else request.decision + "ed", corrected_value,
                 request.reviewer, request.note, utc_now(), review_id),
            )
            previous = connection.execute(
                "SELECT entry_hash FROM audit_log WHERE run_id=? ORDER BY id DESC LIMIT 1", (run_id,)
            ).fetchone()
            entry = {
                "run_id": run_id,
                "review_id": review_id,
                "metric_key": review["metric_key"],
                "original_value": review["original_value"],
                "corrected_value": corrected_value,
                "decision": request.decision,
                "reviewer": request.reviewer,
                "note": request.note,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "previous_hash": previous["entry_hash"] if previous else "0" * 64,
            }
            entry_hash = self.db.audit_hash(entry)
            connection.execute(
                """INSERT INTO audit_log(
                       run_id, review_id, metric_key, original_value, corrected_value, decision,
                       reviewer, note, timestamp, previous_hash, entry_hash
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (*entry.values(), entry_hash),
            )

        result = self.agent.resume(run_id, {
            "metric_key": review["metric_key"],
            "decision": request.decision,
            "corrected_value": corrected_value,
            "reviewer": request.reviewer,
        })
        self._sync_agent_result(run_id, result)
        return self.db.get_run(run_id)

    def close(self) -> None:
        self.agent.close()
