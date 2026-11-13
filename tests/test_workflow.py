import copy
import json
import os
import sqlite3
import tempfile
import unittest
from io import BytesIO
from unittest.mock import patch

import httpx
from docx import Document
from pypdf import PdfReader

from app.assistant import DashboardAssistant
from app.db import Database
from app.explanations import ExplanationGenerator
from app.extractor import FilingExtractor
from app.mcp_client import EdgarMCPClient
from app.models import ReviewRequest
from app.reports import generate_report
from app.sec import SecClient
from app.workflow import AuditWorkflow


class CountingSecClient(SecClient):
    def __init__(self):
        super().__init__()
        self.loads = 0

    def load(self, ticker):
        self.loads += 1
        return super().load(ticker)


class LocalMCPClient:
    def __init__(self, sec):
        self.sec = sec
        self.facts = {}

    def call(self, tool_name, arguments):
        ticker = arguments["ticker"]
        if tool_name == "retrieve_10k_filing":
            filing = self.sec.load(ticker)
            self.facts[ticker] = filing.company_facts
            return {
                "company": filing.company,
                "accession": filing.accession,
                "report_date": filing.report_date,
                "url": filing.url,
                "html": filing.html,
                "offline": filing.offline,
            }
        if tool_name == "lookup_xbrl_company_facts":
            return {"ticker": ticker, "company_facts": self.facts[ticker]}
        raise ValueError(tool_name)


class ExplanationGeneratorTests(unittest.TestCase):
    @staticmethod
    def metric():
        return {
            "key": "total_liabilities",
            "label": "Total Liabilities",
            "value": 35_000_000,
            "previous_value": 32_000_000,
            "unit": "USD",
            "period": "2024-12-31",
            "start_date": None,
            "end_date": "2024-12-31",
            "context_ref": "instant2024",
            "status": "pending",
            "original_value": 35_000_000,
            "confidence": 0.96,
            "confidence_reasons": ["Tagged directly in the filing."],
            "validations": [{
                "rule": "accounting_equation",
                "status": "failed",
                "message": "Assets − liabilities − equity = 5,000,000.00 (tolerance 100,000.00).",
            }],
            "source_name": "us-gaap:Liabilities",
            "source_snippet": "Total liabilities at December 31, 2024: $35 million.",
            "ground_truth": {"value": 30_000_000, "tag": "us-gaap:Liabilities"},
        }

    @staticmethod
    def healthy_metric():
        metric = ExplanationGeneratorTests.metric()
        metric.update({
            "key": "revenue",
            "label": "Revenue",
            "value": 100_000_000,
            "original_value": 100_000_000,
            "previous_value": 90_000_000,
            "status": "auto_approved",
            "start_date": "2024-01-01",
            "context_ref": "duration2024",
            "confidence": 0.98,
            "confidence_reasons": [],
            "validations": [{
                "rule": "year_on_year",
                "status": "passed",
                "message": "Year-on-year change is 11.1%; threshold is 50.0%.",
            }],
            "source_name": "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
            "source_snippet": "Revenue for the year ended December 31, 2024: $100 million.",
            "ground_truth": {"value": 100_000_000, "tag": "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"},
        })
        return metric

    @staticmethod
    def llm_item(metric):
        healthy = metric["status"] == "auto_approved"
        return {
            "metric_key": metric["metric_key"],
            "summary": f"{metric['label']} has status {metric['status']} after validation.",
            "extraction_reason": (
                f"Selected {metric['source_tag']} from filing evidence "
                f"for {metric['period']} in {metric['unit']}."
            ),
            "possible_causes": [] if healthy else ["The reporting context or disclosed value may be inconsistent."],
            "review_guidance": (
                "No human review is required." if healthy
                else "Inspect the evidence and independent XBRL value before deciding."
            ),
        }

    def test_parses_mocked_llm_json_with_attribution(self):
        requests = []
        reason = "The liability tag in the instant2024 context supplies the disclosed USD amount."

        def handler(request):
            requests.append(request)
            payload = json.loads(json.loads(request.content)["messages"][1]["content"])
            metric = payload["metrics"][0]
            content = json.dumps({"explanations": [{
                "metric_key": metric["metric_key"],
                "summary": "The accounting equation does not reconcile by 5,000,000 USD.",
                "extraction_reason": reason,
                "possible_causes": ["A component may use an inconsistent reporting context."],
                "review_guidance": "Compare the liability fact and context with the filing and XBRL value.",
            }]})
            return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

        generator = ExplanationGenerator(
            api_key="test-key",
            model="test-model",
            base_url="https://llm.example/v1",
            transport=httpx.MockTransport(handler),
        )
        explanation = generator.explain_metrics([self.metric()])["total_liabilities"]
        self.assertEqual(explanation["generated_by"], "llm", explanation)
        self.assertEqual(explanation["provider"], "openai_compatible")
        self.assertEqual(explanation["model"], "test-model")
        self.assertEqual(explanation["extraction_reason"], reason)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].headers["Authorization"], "Bearer test-key")
        sent = json.loads(json.loads(requests[0].content)["messages"][1]["content"])
        self.assertEqual(set(sent), {"metrics"})
        self.assertEqual(sent["metrics"][0]["value"], 35_000_000)
        self.assertEqual(sent["metrics"][0]["status"], "pending")
        self.assertEqual(sent["metrics"][0]["context_ref"], "instant2024")
        self.assertIsNone(sent["metrics"][0]["start_date"])
        self.assertEqual(sent["metrics"][0]["end_date"], "2024-12-31")
        self.assertEqual(sent["metrics"][0]["validations"], self.metric()["validations"])

    def test_malformed_and_failed_llm_responses_use_labeled_fallbacks(self):
        for status, content in [(200, "not valid JSON"), (503, "provider unavailable")]:
            with self.subTest(status=status):
                generator = ExplanationGenerator(
                    api_key="test-key",
                    transport=httpx.MockTransport(lambda request: httpx.Response(status, text=content)),
                )
                explanation = generator.explain_metrics([self.metric()])["total_liabilities"]
                self.assertEqual(explanation["generated_by"], "rule_based_fallback")
                self.assertEqual(explanation["provider"], "deterministic_rules")
                self.assertIn("llm_error", explanation)
                self.assertIn("5,000,000", explanation["summary"])
                self.assertTrue(explanation["extraction_reason"].strip())

    def test_missing_api_key_never_calls_provider(self):
        requests = []

        def unexpected_call(request):
            requests.append(request)
            raise AssertionError("Provider should not be called without an API key")

        with patch.dict(os.environ, {"LLM_API_KEY": "", "OPENAI_API_KEY": ""}):
            generator = ExplanationGenerator(transport=httpx.MockTransport(unexpected_call))
            explanation = generator.explain_metrics([self.metric()])["total_liabilities"]
        self.assertFalse(generator.configured)
        self.assertEqual(requests, [])
        self.assertEqual(explanation["generated_by"], "rule_based_fallback")
        self.assertTrue(explanation["extraction_reason"].strip())
        self.assertNotIn("llm_error", explanation)

    def test_omitted_or_invalid_reason_falls_back_only_for_that_metric(self):
        cases = [
            "omitted_metric", "omitted_reason", None, False, 42, [], {}, "", " \t\n",
        ]
        for invalid_reason in cases:
            with self.subTest(invalid_reason=invalid_reason):
                requests = []
                valid_items = {}

                def handler(request):
                    requests.append(request)
                    sent = json.loads(json.loads(request.content)["messages"][1]["content"])["metrics"]
                    items = [self.llm_item(metric) for metric in sent]
                    valid_items.update({item["metric_key"]: copy.deepcopy(item) for item in items})
                    if invalid_reason == "omitted_metric":
                        items = [item for item in items if item["metric_key"] != "total_liabilities"]
                    else:
                        affected = next(item for item in items if item["metric_key"] == "total_liabilities")
                        if invalid_reason == "omitted_reason":
                            affected.pop("extraction_reason")
                        else:
                            affected["extraction_reason"] = invalid_reason
                    content = json.dumps({"explanations": items})
                    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

                generator = ExplanationGenerator(api_key="test-key", transport=httpx.MockTransport(handler))
                metrics = [self.metric(), self.healthy_metric()]
                before = copy.deepcopy(metrics)
                explanations = generator.explain_metrics(metrics)
                self.assertEqual(set(explanations), {"total_liabilities", "revenue"})
                self.assertEqual(len(requests), 1)
                self.assertEqual(metrics, before)
                fallback = explanations["total_liabilities"]
                self.assertEqual(fallback["generated_by"], "rule_based_fallback")
                self.assertEqual(fallback["provider"], "deterministic_rules")
                self.assertTrue(fallback["extraction_reason"].strip())
                self.assertIn("llm_error", fallback)
                healthy = explanations["revenue"]
                self.assertEqual(healthy["generated_by"], "llm")
                self.assertEqual(healthy["summary"], valid_items["revenue"]["summary"])
                self.assertEqual(healthy["extraction_reason"], valid_items["revenue"]["extraction_reason"])
                self.assertEqual(healthy["possible_causes"], [])
                self.assertNotIn("llm_error", healthy)

    def test_explanation_generation_does_not_mutate_source_metrics(self):
        for mode in ("llm", "malformed_response", "no_provider"):
            with self.subTest(mode=mode):
                requests = []

                def handler(request):
                    requests.append(request)
                    sent = json.loads(json.loads(request.content)["messages"][1]["content"])["metrics"]
                    content = (
                        "not valid JSON" if mode == "malformed_response"
                        else json.dumps({"explanations": [self.llm_item(metric) for metric in sent]})
                    )
                    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

                metrics = [self.metric(), self.healthy_metric()]
                before = copy.deepcopy(metrics)
                with patch.dict(os.environ, {"LLM_API_KEY": "", "OPENAI_API_KEY": ""}):
                    generator = ExplanationGenerator(
                        api_key=None if mode == "no_provider" else "test-key",
                        transport=httpx.MockTransport(handler),
                    )
                    explanations = generator.explain_metrics(metrics)
                self.assertEqual(metrics, before)
                self.assertEqual(len(requests), 0 if mode == "no_provider" else 1)
                self.assertEqual(set(explanations), {metric["key"] for metric in metrics})
                for explanation in explanations.values():
                    self.assertEqual(explanation["generated_by"], "llm" if mode == "llm" else "rule_based_fallback")
                    self.assertTrue(explanation["extraction_reason"].strip())
                    self.assertLessEqual(len(explanation["possible_causes"]), 3)

    def test_no_provider_healthy_fallback_does_not_invent_an_anomaly_or_require_review(self):
        def unexpected_call(request):
            self.fail("Provider should not be called without an API key")

        metric = self.healthy_metric()
        before = copy.deepcopy(metric)
        with patch.dict(os.environ, {"LLM_API_KEY": "", "OPENAI_API_KEY": ""}):
            generator = ExplanationGenerator(transport=httpx.MockTransport(unexpected_call))
            explanation = generator.explain_metrics([metric])["revenue"]
        self.assertEqual(metric, before)
        self.assertEqual(metric["status"], "auto_approved")
        self.assertEqual(explanation["generated_by"], "rule_based_fallback")
        self.assertEqual(explanation["provider"], "deterministic_rules")
        self.assertEqual(explanation["possible_causes"], [])
        self.assertTrue(explanation["extraction_reason"].strip())
        self.assertRegex(explanation["extraction_reason"].lower(), r"tag|xbrl|filing|source|context")
        text = " ".join(explanation[field] for field in ("summary", "extraction_reason", "review_guidance")).lower()
        self.assertNotRegex(text, r"requires? (?:human |manual )?review|must (?:be reviewed|review)|before approv")
        self.assertNotRegex(text, r"may be (?:ambiguous|incorrect|inconsistent)|(?<!no )failed validation")
        self.assertNotIn("llm_error", explanation)

    def test_missing_value_fallback_explains_absence_without_inventing_figures(self):
        metric = self.metric()
        metric.update({
            "value": None,
            "original_value": None,
            "previous_value": None,
            "confidence": 0.0,
            "confidence_reasons": ["Required fact is missing or has an unsupported period/unit"],
            "context_ref": None,
            "source_snippet": "No matching consolidated annual inline-XBRL fact was found in the filing.",
            "ground_truth": {"value": 987_654_321, "tag": "us-gaap:Liabilities"},
            "validations": [{
                "rule": "extraction", "status": "failed",
                "message": "No usable value was extracted; reviewer input is required.",
            }],
        })
        before = copy.deepcopy(metric)
        with patch.dict(os.environ, {"LLM_API_KEY": "", "OPENAI_API_KEY": ""}):
            generator = ExplanationGenerator(transport=httpx.MockTransport(lambda request: self.fail("Unexpected provider call")))
            explanation = generator.explain_metrics([metric])["total_liabilities"]
        self.assertEqual(metric, before)
        self.assertIsNone(metric["value"])
        self.assertEqual(metric["status"], "pending")
        self.assertEqual(explanation["generated_by"], "rule_based_fallback")
        self.assertNotIn("llm_error", explanation)
        self.assertTrue(explanation["extraction_reason"].strip())
        self.assertRegex(explanation["extraction_reason"].lower(), r"missing|absen|no (?:matching|usable|value)|not (?:found|extracted|available)|unavailable")
        text = " ".join(explanation[field] for field in ("summary", "extraction_reason", "review_guidance"))
        self.assertNotRegex(text, r"987[\d,]*654[\d,]*321")
        self.assertNotRegex(text, r"\b\d[\d,]*(?:\.\d+)?\s*(?:USD|dollars|million|billion)")


class DashboardAssistantTests(unittest.TestCase):
    @staticmethod
    def selected_run():
        metric = ExplanationGeneratorTests.metric()
        metric.update({
            "value": 30_000_000,
            "original_value": 35_000_000,
            "status": "corrected",
            "verification": {"status": "matched", "matches": True, "relative_error": 0},
            "explanation": {
                "summary": "The accounting equation failed.",
                "extraction_reason": "The initial liability value came from the filing's instant2024 liability tag.",
                "possible_causes": ["A component may be mis-tagged."],
                "review_guidance": "Inspect the evidence.",
                "generated_by": "llm",
            },
        })
        return {
            "id": "run-1",
            "ticker": "DEMO",
            "company_name": "Demo Manufacturing Corp.",
            "status": "completed",
            "stage": "complete",
            "error": None,
            "created_at": "2026-11-12T00:00:00+00:00",
            "state": {
                "company": {"ticker": "DEMO", "name": "Demo Manufacturing Corp."},
                "filing": {"accession": "demo-1", "report_date": "2024-12-31", "offline": True},
                "confidence_threshold": 0.85,
                "variance_threshold": 0.50,
                "metrics": [metric],
                "report": {
                    "accuracy_percentage": 100.0,
                    "ground_truth_matches": 1,
                    "ground_truth_compared": 1,
                },
                "agent": {
                    "framework": "LangGraph",
                    "checkpoint_store": "SQLite",
                    "mcp_transport": "stdio",
                    "next_action": "end",
                    "available_tools": ["retrieve_filing"],
                    "tool_trace": [{"selected_tool": "verify_ground_truth"}],
                },
            },
            "reviews": [{"status": "corrected", "metric_key": "total_liabilities"}],
            "audit_log": [{
                "metric_key": "total_liabilities",
                "decision": "correct",
                "reviewer": "test-reviewer",
                "original_value": 35_000_000,
                "corrected_value": 30_000_000,
            }],
            "audit_verification": {"valid": True},
        }

    def test_mocked_llm_receives_dashboard_context_and_history(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"choices": [{"message": {"content": "Total Liabilities was corrected to 30,000,000 USD."}}]})

        assistant = DashboardAssistant(
            api_key="test-key",
            model="test-model",
            base_url="https://llm.example/v1",
            transport=httpx.MockTransport(handler),
        )
        history = [
            {"role": "user", "content": "Which value changed?"},
            {"role": "assistant", "content": "Total Liabilities changed."},
        ]
        result = assistant.answer("What is its final value?", self.selected_run(), history)
        self.assertEqual(result["generated_by"], "llm")
        self.assertEqual(result["model"], "test-model")
        payload = json.loads(requests[0].content)
        self.assertEqual([message["role"] for message in payload["messages"]], ["system", "user", "assistant", "user"])
        dashboard_message = payload["messages"][-1]["content"]
        self.assertIn("30000000", dashboard_message)
        self.assertIn("total_liabilities", dashboard_message)
        self.assertIn("What is its final value?", dashboard_message)
        self.assertNotIn("test-key", dashboard_message)

    def test_fallback_answers_from_selected_run_without_provider(self):
        def unexpected_call(request):
            raise AssertionError("Provider should not be called without an API key")

        with patch.dict(os.environ, {"LLM_API_KEY": "", "OPENAI_API_KEY": ""}):
            assistant = DashboardAssistant(transport=httpx.MockTransport(unexpected_call))
            result = assistant.answer("What is total liabilities now?", self.selected_run(), [])
        self.assertEqual(result["generated_by"], "rule_based_fallback")
        self.assertIn("30,000,000 USD", result["answer"])
        self.assertIn("corrected", result["answer"])
        self.assertIn("35,000,000 USD", result["answer"])

    def test_provider_failure_returns_grounded_fallback(self):
        assistant = DashboardAssistant(
            api_key="test-key",
            transport=httpx.MockTransport(lambda request: httpx.Response(503, text="unavailable")),
        )
        result = assistant.answer("What is the XBRL accuracy?", self.selected_run(), [])
        self.assertEqual(result["generated_by"], "rule_based_fallback")
        self.assertIn("100.0%", result["answer"])
        self.assertIn("1 of 1", result["answer"])
        self.assertIn("llm_error", result)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {"LLM_API_KEY": "", "OPENAI_API_KEY": ""})
        self.environment.start()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.temp_dir.name, "test.db"))
        self.sec = CountingSecClient()
        self.workflow = AuditWorkflow(self.db, self.sec, mcp_client=LocalMCPClient(self.sec))

    def tearDown(self):
        self.workflow.close()
        self.temp_dir.cleanup()
        self.environment.stop()

    def decide(self, run, review, decision="approve", value=None):
        request = ReviewRequest(decision=decision, corrected_value=value, reviewer="test-reviewer", note="test decision")
        return self.workflow.submit_decision(run["id"], review["id"], request)

    def complete_demo(self, run):
        for review in self.db.get_reviews(run["id"]):
            if review["metric_key"] == "total_liabilities":
                run = self.decide(run, review, "correct", 30_000_000)
            else:
                run = self.decide(run, review)
        return run

    def test_extracts_eight_metrics_with_evidence(self):
        filing = self.sec.load("DEMO")
        extractor = FilingExtractor(filing.html, filing.report_date, filing.url)
        metrics = extractor.extract()
        self.assertEqual(len(metrics), 8)
        self.assertTrue(all(metric["unit"] for metric in metrics))
        self.assertTrue(all(metric["period"] == "2024-12-31" for metric in metrics))
        self.assertTrue(all(metric["source_snippet"] for metric in metrics))
        self.assertEqual(metrics[0]["value"], 100_000_000)
        self.assertEqual(metrics[-1]["value"], 4.0)
        self.assertEqual(extractor.segment_total(metrics[0])["total"], 100_000_000)

    def test_derives_untagged_liabilities_with_reduced_confidence(self):
        filing = self.sec.load("DEMO")
        start = filing.html.index("<tr><td>Total liabilities at December 31, 2024")
        end = filing.html.index("</tr>", start) + 5
        filing.html = filing.html[:start] + filing.html[end:]
        extractor = FilingExtractor(filing.html, filing.report_date, filing.url)
        liabilities = extractor.extract()[3]
        self.assertEqual(liabilities["value"], 30_000_000)
        self.assertEqual(liabilities["confidence"], 0.70)
        self.assertIn("Derived", liabilities["source_name"])
        self.assertIn("assets minus total equity", liabilities["source_snippet"])

    def test_pauses_resumes_and_verifies_without_refetching(self):
        run = self.workflow.start("DEMO")
        self.assertEqual(run["status"], "awaiting_review")
        self.assertEqual(len(self.db.get_reviews(run["id"])), 4)
        self.assertEqual(self.sec.loads, 1)
        agent = run["state"]["agent"]
        self.assertEqual(agent["framework"], "LangGraph")
        self.assertEqual(agent["next_action"], "human_review")
        self.assertFalse(agent["explanations"]["llm_configured"])
        self.assertEqual(
            [event["selected_tool"] for event in agent["tool_trace"][:6]],
            ["retrieve_filing", "lookup_xbrl_company_facts", "extract_metrics", "validate_metrics", "explain_metrics", "human_review"],
        )
        self.assertIn("explain_metrics", agent["available_tools"])
        self.assertNotIn("explain_anomalies", agent["available_tools"])
        metrics = run["state"]["metrics"]
        self.assertEqual(len(metrics), 8)
        self.assertEqual(sum(metric["status"] == "pending" for metric in metrics), 4)
        self.assertEqual(sum(metric["status"] == "auto_approved" for metric in metrics), 4)
        self.assertTrue(all(metric["explanation"]["generated_by"] == "rule_based_fallback" for metric in metrics))
        self.assertTrue(all(metric["explanation"]["extraction_reason"].strip() for metric in metrics))
        run = self.complete_demo(run)
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["state"]["report"]["accuracy_percentage"], 100.0)
        self.assertEqual(run["state"]["report"]["automatically_approved"], 4)
        self.assertEqual(run["state"]["report"]["escalated"], 4)
        self.assertEqual(run["state"]["report"]["completed_reviews"], 4)
        self.assertEqual(run["state"]["report"]["pending_reviews"], 0)
        self.assertEqual(
            [event["selected_tool"] for event in run["state"]["agent"]["tool_trace"]].count("explain_metrics"), 1,
        )
        self.assertEqual(self.sec.loads, 1)
        self.assertEqual(len(self.db.get_audit(run["id"])), 4)
        self.assertTrue(self.db.verify_audit(run["id"])["valid"])

    def test_all_eight_llm_reasons_persist_through_correction_and_restart(self):
        requests = []
        response_items = {}

        def handler(request):
            requests.append(request)
            metrics = json.loads(json.loads(request.content)["messages"][1]["content"])["metrics"]
            items = [ExplanationGeneratorTests.llm_item(metric) for metric in metrics]
            response_items.update({item["metric_key"]: item for item in items})
            content = json.dumps({"explanations": items})
            return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

        self.workflow.close()
        generator = ExplanationGenerator(
            api_key="test-key",
            model="mock-auditor-model",
            transport=httpx.MockTransport(handler),
        )
        self.workflow = AuditWorkflow(
            self.db,
            self.sec,
            mcp_client=LocalMCPClient(self.sec),
            explanation_generator=generator,
        )
        run = self.workflow.start("DEMO")
        self.assertEqual(run["status"], "awaiting_review")
        self.assertEqual(len(self.db.get_reviews(run["id"])), 4)
        self.assertEqual(len(requests), 1)
        sent = json.loads(json.loads(requests[0].content)["messages"][1]["content"])
        self.assertEqual(set(sent), {"metrics"})
        self.assertEqual(len(sent["metrics"]), 8)
        self.assertEqual(sum(metric["status"] == "pending" for metric in sent["metrics"]), 4)
        self.assertEqual(sum(metric["status"] == "auto_approved" for metric in sent["metrics"]), 4)
        original_metrics = {metric["key"]: copy.deepcopy(metric) for metric in run["state"]["metrics"]}
        self.assertEqual({metric["metric_key"] for metric in sent["metrics"]}, set(original_metrics))
        for metric in sent["metrics"]:
            original = original_metrics[metric["metric_key"]]
            for field in ("value", "previous_value", "unit", "period", "status", "confidence", "validations"):
                self.assertEqual(metric[field], original[field])
            self.assertTrue(metric["validations"])
        saved_explanations = {key: metric["explanation"] for key, metric in original_metrics.items()}
        self.assertEqual(len(saved_explanations), 8)
        for key, explanation in saved_explanations.items():
            self.assertEqual(explanation["generated_by"], "llm")
            self.assertEqual(explanation["model"], "mock-auditor-model")
            self.assertEqual(explanation["summary"], response_items[key]["summary"])
            self.assertEqual(explanation["extraction_reason"], response_items[key]["extraction_reason"])
            self.assertTrue(explanation["extraction_reason"].strip())
            self.assertLessEqual(len(explanation["possible_causes"]), 3)
            if original_metrics[key]["status"] == "auto_approved":
                self.assertEqual(explanation["possible_causes"], [])

        review = next(item for item in self.db.get_reviews(run["id"]) if item["metric_key"] == "total_liabilities")
        run = self.decide(run, review, "correct", 30_000_000)
        self.assertEqual(run["status"], "awaiting_review")
        corrected = next(metric for metric in run["state"]["metrics"] if metric["key"] == "total_liabilities")
        self.assertEqual(corrected["value"], 30_000_000)
        self.assertEqual(corrected["original_value"], 35_000_000)
        self.assertEqual(corrected["status"], "corrected")
        self.assertEqual(corrected["explanation"], saved_explanations["total_liabilities"])
        self.assertEqual(len(requests), 1)

        self.workflow.close()
        restarted_sec = CountingSecClient()
        self.workflow = AuditWorkflow(
            self.db,
            restarted_sec,
            mcp_client=LocalMCPClient(restarted_sec),
            explanation_generator=generator,
        )
        persisted = self.db.get_run(run["id"])
        self.assertEqual(
            {metric["key"]: metric["explanation"] for metric in persisted["state"]["metrics"]},
            saved_explanations,
        )
        for review in self.db.get_reviews(run["id"]):
            if review["status"] == "pending":
                run = self.decide(run, review)
                self.assertEqual(
                    {metric["key"]: metric["explanation"] for metric in run["state"]["metrics"]},
                    saved_explanations,
                )
                self.assertEqual(len(requests), 1)
        self.assertEqual(run["status"], "completed")
        self.assertEqual(len(run["state"]["metrics"]), 8)
        self.assertEqual(self.sec.loads, 1)
        self.assertEqual(restarted_sec.loads, 0)
        self.assertEqual(len(self.db.get_audit(run["id"])), 4)
        self.assertTrue(self.db.verify_audit(run["id"])["valid"])
        self.assertEqual(run["state"]["report"]["accuracy_percentage"], 100.0)
        self.assertEqual(run["state"]["report"]["automatically_approved"], 4)
        self.assertEqual(run["state"]["report"]["escalated"], 4)
        self.assertEqual(run["state"]["report"]["completed_reviews"], 4)
        self.assertEqual(
            [event["selected_tool"] for event in run["state"]["agent"]["tool_trace"]].count("explain_metrics"), 1,
        )
        for metric in run["state"]["metrics"]:
            original = original_metrics[metric["key"]]
            expected = copy.deepcopy(original)
            expected["status"] = (
                "corrected" if metric["key"] == "total_liabilities"
                else "approved" if original["status"] == "pending" else "auto_approved"
            )
            if metric["key"] == "total_liabilities":
                expected["value"] = 30_000_000
            self.assertEqual({key: value for key, value in metric.items() if key != "verification"}, expected)
        self.assertEqual(
            {metric["key"]: metric["explanation"] for metric in self.db.get_run(run["id"])["state"]["metrics"]},
            saved_explanations,
        )

    def test_resume_uses_persisted_state_after_workflow_restart(self):
        run = self.workflow.start("DEMO")
        restarted_sec = CountingSecClient()
        restarted = AuditWorkflow(self.db, restarted_sec, mcp_client=LocalMCPClient(restarted_sec))
        try:
            for review in self.db.get_reviews(run["id"]):
                request = ReviewRequest(
                    decision="correct" if review["metric_key"] == "total_liabilities" else "approve",
                    corrected_value=30_000_000 if review["metric_key"] == "total_liabilities" else None,
                    reviewer="restart-reviewer",
                )
                run = restarted.submit_decision(run["id"], review["id"], request)
            self.assertEqual(run["status"], "completed")
            self.assertEqual(restarted_sec.loads, 0)
            self.assertEqual(run["state"]["report"]["accuracy_percentage"], 100.0)
            self.assertEqual(run["state"]["agent"]["checkpoint_store"], "SQLite")
        finally:
            restarted.close()

    def test_duplicate_decision_does_not_repeat_side_effects(self):
        run = self.workflow.start("DEMO")
        review = self.db.get_reviews(run["id"])[0]
        self.decide(run, review)
        with self.assertRaisesRegex(ValueError, "already been decided"):
            self.decide(run, review)
        self.assertEqual(len(self.db.get_audit(run["id"])), 1)
        self.assertEqual(self.sec.loads, 1)

    def test_audit_log_rejects_updates_and_deletes(self):
        run = self.workflow.start("DEMO")
        review = self.db.get_reviews(run["id"])[0]
        self.decide(run, review)
        with self.db.connect() as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE audit_log SET decision='reject'")
        with self.db.connect() as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM audit_log")

    def test_all_eligible_values_auto_approve_and_finalize(self):
        filing = self.sec.load("DEMO")
        filing.html = filing.html.replace(
            'name="us-gaap:Liabilities" contextRef="instant2024" unitRef="USD" scale="6">35<',
            'name="us-gaap:Liabilities" contextRef="instant2024" unitRef="USD" scale="6">30<',
        )
        start = filing.html.index("<tr><td>Conflicting cash disclosure")
        end = filing.html.index("</tr>", start) + 5
        filing.html = filing.html[:start] + filing.html[end:]
        self.sec.load = lambda ticker: filing
        requests = []
        response_items = {}

        def handler(request):
            requests.append(request)
            sent = json.loads(json.loads(request.content)["messages"][1]["content"])["metrics"]
            items = [ExplanationGeneratorTests.llm_item(metric) for metric in sent]
            response_items.update({item["metric_key"]: item for item in items})
            content = json.dumps({"explanations": items})
            return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

        self.workflow.close()
        self.workflow = AuditWorkflow(
            self.db,
            self.sec,
            mcp_client=LocalMCPClient(self.sec),
            explanation_generator=ExplanationGenerator(api_key="test-key", transport=httpx.MockTransport(handler)),
        )
        run = self.workflow.start("DEMO")
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["state"]["report"]["automatically_approved"], 8)
        self.assertEqual(run["state"]["report"]["escalated"], 0)
        self.assertEqual(run["state"]["report"]["completed_reviews"], 0)
        self.assertEqual(self.db.get_reviews(run["id"]), [])
        self.assertEqual(self.db.get_audit(run["id"]), [])
        self.assertEqual(run["state"]["report"]["accuracy_percentage"], 100.0)
        self.assertEqual(len(requests), 1)
        sent = json.loads(json.loads(requests[0].content)["messages"][1]["content"])["metrics"]
        self.assertEqual(len(sent), 8)
        self.assertTrue(all(metric["status"] == "auto_approved" for metric in sent))
        self.assertTrue(all(metric["validations"] for metric in sent))
        self.assertEqual(len(run["state"]["metrics"]), 8)
        for metric in run["state"]["metrics"]:
            self.assertEqual(metric["status"], "auto_approved")
            self.assertEqual(metric["explanation"]["generated_by"], "llm")
            self.assertEqual(metric["explanation"]["summary"], response_items[metric["key"]]["summary"])
            self.assertEqual(metric["explanation"]["extraction_reason"], response_items[metric["key"]]["extraction_reason"])
            self.assertEqual(metric["explanation"]["possible_causes"], [])
        self.assertEqual(
            [event["selected_tool"] for event in run["state"]["agent"]["tool_trace"]],
            ["retrieve_filing", "lookup_xbrl_company_facts", "extract_metrics", "validate_metrics", "explain_metrics", "verify_ground_truth", "end"],
        )

    def test_pdf_and_docx_exports_include_all_ai_reasons_without_changing_run(self):
        explanations = {}
        generator_inputs = []

        def explain_metrics(metrics):
            generator_inputs.append(copy.deepcopy(metrics))
            explanations.update({metric["key"]: {
                "summary": f"AI summary for {metric['key']}.",
                "extraction_reason": f"AI reason for {metric['key']}: selected the supplied filing tag and period.",
                "possible_causes": [] if metric["status"] == "auto_approved" else ["The disclosed context may need review."],
                "review_guidance": "Use the stored evidence and validation results.",
                "generated_by": "llm",
                "provider": "openai_compatible",
                "model": "mock-report-model",
            } for metric in metrics})
            return copy.deepcopy(explanations)

        with patch.object(self.workflow.explainer, "explain_metrics", side_effect=explain_metrics) as mocked_generator:
            run = self.workflow.start("DEMO")
            self.assertEqual(run["status"], "awaiting_review")
            self.assertEqual(len(generator_inputs), 1)
            self.assertEqual(len(generator_inputs[0]), 8)
            self.assertEqual(len(explanations), 8)
            self.assertEqual(len(self.db.get_reviews(run["id"])), 4)
            self.assertEqual(sum(metric["status"] == "auto_approved" for metric in run["state"]["metrics"]), 4)
            self.assertEqual(
                [{key: value for key, value in metric.items() if key != "explanation"} for metric in run["state"]["metrics"]],
                generator_inputs[0],
            )
            run["reviews"] = self.db.get_reviews(run["id"])
            run["audit_log"] = self.db.get_audit(run["id"])
            run["audit_verification"] = self.db.verify_audit(run["id"])
            before = copy.deepcopy(run)
            saved_run = self.db.get_run(run["id"])
            loads = self.sec.loads
            for format in ("pdf", "docx"):
                with self.subTest(format=format):
                    output = generate_report(run, format)
                    if format == "pdf":
                        document = PdfReader(BytesIO(output))
                        text = "\n".join(page.extract_text() or "" for page in document.pages)
                    else:
                        document = Document(BytesIO(output))
                        text = "\n".join([
                            *(paragraph.text for paragraph in document.paragraphs),
                            *(cell.text for table in document.tables for row in table.rows for cell in row.cells),
                        ])
                    text = " ".join(text.split())
                    for key, explanation in explanations.items():
                        with self.subTest(format=format, metric=key):
                            self.assertIn(explanation["summary"], text)
                            self.assertIn(explanation["extraction_reason"], text)
                    self.assertEqual(run, before)
                    self.assertEqual(self.db.get_run(run["id"]), saved_run)
                    self.assertEqual(self.db.get_reviews(run["id"]), before["reviews"])
                    self.assertEqual(self.db.get_audit(run["id"]), before["audit_log"])
                    self.assertEqual(self.sec.loads, loads)
                    mocked_generator.assert_called_once()


class MCPIntegrationTests(unittest.TestCase):
    def test_mcp_server_exposes_filing_and_xbrl_tools(self):
        client = EdgarMCPClient()
        tools = client.list_tools()
        self.assertEqual(
            {tool["name"] for tool in tools},
            {"retrieve_10k_filing", "lookup_xbrl_company_facts"},
        )
        filing = client.call("retrieve_10k_filing", {"ticker": "DEMO"})
        self.assertEqual(filing["accession"], "0000000000-24-000001")
        self.assertIn("ix:nonFraction", filing["html"])
        facts = client.call("lookup_xbrl_company_facts", {"ticker": "DEMO"})
        self.assertEqual(len(facts["company_facts"]["facts"]["us-gaap"]), 8)


if __name__ == "__main__":
    unittest.main()
