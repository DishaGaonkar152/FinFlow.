import copy
import importlib
import json
import logging
import os
import secrets
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

import httpx
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.auth import COOKIE_NAME
from app.db import Database
from app.models import ReviewRequest, RunRequest
from app.sec import COMPANIES, SecClient, SecUnavailableError
from app.workflow import AuditWorkflow


FIXTURES = Path(__file__).resolve().parent / "fixtures"
CATALOG_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
CIK = "0001234567"
UNLISTED_CIK = "0007654321"
NO_10K_CIK = "0007654322"
ACCESSION = "0001234567-25-000001"
SUBMISSIONS_URL = f"https://data.sec.gov/submissions/CIK{CIK}.json"
FACTS_URL = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{CIK}.json"
FILING_URL = "https://www.sec.gov/Archives/edgar/data/1234567/000123456725000001/orion-2024.htm"
NAME = "Orion Test Manufacturing, Inc."


def database_dump(path):
    # Explicitly close SQLite handles so TemporaryDirectory works on Windows.
    with closing(sqlite3.connect(path)) as connection:
        return "\n".join(connection.iterdump())


def filing_batch(rows):
    fields = ("form", "accessionNumber", "reportDate", "filingDate", "primaryDocument")
    return {field: [row[index] for row in rows] for index, field in enumerate(fields)}


def annual_batch():
    # Deliberately unordered; neither a newer amendment nor a 20-F is a 10-K.
    return filing_batch([
        ("10-K", "0001234567-24-000001", "2023-12-31", "2024-02-01", "orion-2023.htm"),
        ("20-F", "0001234567-25-000004", "2024-12-31", "2025-05-01", "foreign.htm"),
        ("10-K/A", "0001234567-25-000003", "2024-12-31", "2025-04-01", "amendment.htm"),
        ("10-K", ACCESSION, "2024-12-31", "2025-02-01", "orion-2024.htm"),
    ])


def submissions(cik=CIK, name=NAME, tickers=None, recent=None):
    return {
        "cik": int(cik), "name": name,
        "tickers": ["ORNT", "ORNT.A", "ORNT.B"] if tickers is None else tickers,
        "exchanges": ["NYSE"], "sic": "3999",
        "sicDescription": "Synthetic test manufacturing", "description": "Mock SEC metadata only",
        "entityType": "operating", "fiscalYearEnd": "1231",
        "addresses": {"business": {
            "street1": "1 Test Street", "street2": "Suite 2", "city": "Test City",
            "stateOrCountryDescription": "Test State", "zipCode": "00000",
        }},
        "filings": {"recent": annual_batch() if recent is None else recent, "files": []},
    }


class MockSECSource:
    """Only SEC metadata is synthetic; all financial evidence is the DEMO fixture."""

    def __init__(self):
        # Nonstandard field order verifies the actual fields/data catalog format.
        rows = [
            ["NYSE", NAME, int(CIK), "ORNT"],
            ["NYSE", NAME, int(CIK), "ORNT.A"],
            ["NYSE", NAME, int(CIK), "ORNT.B"],
            ["NASDAQ", "Shared Test Holdings", 2222221, "SHR1"],
            ["NYSE", "Shared Test Holdings", 2222222, "SHR2"],
            ["NYSE", "Dual Class Test Corp.", 3333333, "DUAL.A"],
            ["NYSE", "Dual Class Test Corp.", 3333333, "DUAL.B"],
            ["NYSE", "Zulu Test Industries", 4444444, "ORBIT"],
            ["NASDAQ", "Orbit", 4444445, "EXACT"],
            ["NYSE", "The Orbit Test Company", 4444446, "OTHER"],
        ]
        rows.extend(["NASDAQ", f"Orbit Test {index:02d}", 5000000 + index, f"OT{index:02d}"]
                    for index in range(27))
        self.catalog = {"fields": ["exchange", "name", "cik", "ticker"], "data": list(reversed(rows))}
        self.metadata = submissions()
        self.html = (FIXTURES / "demo_10k.html").read_text(encoding="utf-8")
        self.facts = json.loads((FIXTURES / "demo_companyfacts.json").read_text(encoding="utf-8"))
        self.routes = {
            CATALOG_URL: self.catalog, SUBMISSIONS_URL: self.metadata,
            FACTS_URL: self.facts, FILING_URL: self.html,
            f"https://data.sec.gov/submissions/CIK{UNLISTED_CIK}.json": submissions(
                UNLISTED_CIK, "Unlisted Test Filer LLC", tickers=[]),
            f"https://data.sec.gov/submissions/CIK{NO_10K_CIK}.json": submissions(
                NO_10K_CIK, "Foreign Test Filer", tickers=["FOREIGN"],
                recent=filing_batch([
                    ("20-F", ACCESSION, "2024-12-31", "2025-02-01", "foreign.htm"),
                    ("40-F", ACCESSION, "2024-12-31", "2025-02-02", "foreign40.htm"),
                    ("10-K/A", ACCESSION, "2024-12-31", "2025-02-03", "amendment.htm"),
                ])),
        }
        self.requests = []
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request):
        self.requests.append(request)
        if request.method != "GET" or str(request.url) not in self.routes:
            raise AssertionError(f"Unexpected SEC request: {request.method} {request.url}")
        value = self.routes[str(request.url)]
        if callable(value):
            return value(request)
        if isinstance(value, str):
            return httpx.Response(200, text=value)
        return httpx.Response(200, json=value)

    @property
    def urls(self):
        return [str(request.url) for request in self.requests]

    def fail(self, url, kind):
        def response(request):
            if kind == "timeout":
                raise httpx.ReadTimeout("mock SEC timeout", request=request)
            if kind == "connection":
                raise httpx.ConnectError("mock SEC connection failure", request=request)
            if kind == "invalid_json":
                return httpx.Response(200, text="<html>not SEC JSON</html>")
            return httpx.Response(kind, text="mock SEC HTTP failure")
        self.routes[url] = response

    def archive(self, name, batch):
        self.metadata["filings"]["files"].append({"name": name})
        url = f"https://data.sec.gov/submissions/{name}"
        self.routes[url] = batch
        return url


class InProcessMCP:
    """Exercise the real MCP tool functions without a subprocess or live EDGAR."""

    def __init__(self, sec):
        self.sec = sec
        self.calls = []

    def list_tools(self):
        return [{"name": name} for name in ("retrieve_10k_filing", "lookup_xbrl_company_facts")]

    def call(self, name, arguments):
        from app import mcp_server
        self.calls.append((name, dict(arguments)))
        functions = {
            "retrieve_10k_filing": mcp_server.retrieve_10k_filing,
            "lookup_xbrl_company_facts": mcp_server.lookup_xbrl_company_facts,
        }
        if name not in functions or set(arguments) != {"ticker"}:
            raise AssertionError(f"Unexpected MCP call: {name} {arguments}")
        with patch.object(mcp_server, "sec", self.sec):
            return functions[name](**arguments)


class OfflineTests(unittest.TestCase):
    def setUp(self):
        super().setUp()
        environment = patch.dict(os.environ, {"LLM_API_KEY": "", "OPENAI_API_KEY": ""})
        environment.start()
        self.addCleanup(environment.stop)
        quiet_http = patch.object(logging.getLogger("httpx"), "disabled", True)
        quiet_http.start()
        self.addCleanup(quiet_http.stop)
        # A missing MockTransport must fail loudly, not reach the internet.
        for target in ("httpx.HTTPTransport.handle_request", "httpx.AsyncHTTPTransport.handle_async_request"):
            guard = patch(target, side_effect=AssertionError("Real network is forbidden in SEC regression tests"))
            guard.start()
            self.addCleanup(guard.stop)
        self.source = MockSECSource()
        self.sec = SecClient(user_agent="SECRegressionTests test@example.invalid", transport=self.source.transport)


class SecClientTests(OfflineTests):
    def test_catalog_uses_fields_schema_and_discovers_nonpreset_company(self):
        self.assertNotIn("ORNT", {company["ticker"] for company in COMPANIES})
        results = self.sec.search_companies("orion test")
        self.assertEqual({company["ticker"] for company in results}, {"ORNT", "ORNT.A", "ORNT.B"})
        self.assertTrue(all(company["cik"] == CIK and company["exchange"] == "NYSE" for company in results))
        self.assertEqual(self.source.urls, [CATALOG_URL])
        self.assertEqual(self.source.requests[0].headers["User-Agent"], "SECRegressionTests test@example.invalid")

    def test_search_ranks_ticker_exact_name_prefix_then_contains_and_limits_twenty(self):
        results = self.sec.search_companies("oRbIt")
        self.assertEqual(len(results), 20)
        self.assertEqual([company["ticker"] for company in results[:2]], ["ORBIT", "EXACT"])
        self.assertEqual([company["name"] for company in results[2:]],
                         [f"Orbit Test {index:02d}" for index in range(18)])
        self.assertEqual(self.sec.search_companies("no such synthetic company"), [])

    def test_catalog_cached_until_exact_one_hour_boundary(self):
        with patch("app.sec.monotonic", return_value=100.0) as clock:
            self.sec.search_companies("orion")
            clock.return_value = 3699.999
            self.sec.company(NAME)
            self.sec.search_companies("orbit")
            self.assertEqual(self.source.urls.count(CATALOG_URL), 1)
            clock.return_value = 3700.0
            self.sec.search_companies("orion")
            self.assertEqual(self.source.urls.count(CATALOG_URL), 2)

    def test_search_and_company_return_copies_not_mutable_catalog_records(self):
        self.sec.search_companies("orion")[0]["name"] = "tampered search"
        self.sec.company("ORNT")["name"] = "tampered lookup"
        self.assertEqual(self.sec.company("ORNT")["name"], NAME)
        demo = self.sec.company("DEMO")
        demo["name"] = "tampered preset"
        self.assertEqual(self.sec.company("DEMO")["name"], COMPANIES[0]["name"])

    def test_exact_company_name_and_ticker_are_case_insensitive(self):
        for identifier in (NAME, NAME.lower(), "  " + NAME + "  ", "ornt", "OrNt.A", "ORNT.B"):
            with self.subTest(identifier=identifier):
                self.assertEqual(self.sec.company(identifier)["cik"], CIK)
        for partial in ("Orion", "Orion Test Manufacturing", "no such filer"):
            with self.subTest(partial=partial), self.assertRaisesRegex(ValueError, "No exact SEC company match"):
                self.sec.company(partial)

    def test_ambiguous_exact_names_require_specific_ticker_or_cik(self):
        self.assertEqual({company["ticker"] for company in self.sec.search_companies("Shared Test Holdings")},
                         {"SHR1", "SHR2"})
        with self.assertRaisesRegex(ValueError, "Several SEC filers"):
            self.sec.company("shared test holdings")
        self.assertEqual(self.sec.company("SHR1")["cik"], "0002222221")
        self.assertEqual(self.sec.company("SHR2")["cik"], "0002222222")
        self.assertEqual(self.sec.company("Dual Class Test Corp.")["cik"], "0003333333")

    def test_numeric_and_prefixed_cik_resolve_without_catalog(self):
        for identifier in ("1234567", CIK, "CIK1234567", "cik 1234567", " CIK 0001234567 "):
            with self.subTest(identifier=identifier):
                self.assertEqual(self.sec.company(identifier)["cik"], CIK)
        self.assertEqual(self.source.urls, [])
        for identifier in ("0", "0000000000", "CIK 0"):
            with self.subTest(identifier=identifier), self.assertRaisesRegex(ValueError, "valid SEC CIK"):
                self.sec.company(identifier)
        self.assertEqual(self.source.urls, [])

    def test_cik_search_verifies_unlisted_filer_instead_of_using_catalog(self):
        result = self.sec.search_companies("CIK7654321")
        self.assertEqual(result, [{"ticker": f"CIK{UNLISTED_CIK}", "cik": UNLISTED_CIK,
                                  "name": "Unlisted Test Filer LLC"}])
        self.assertEqual(self.source.urls, [f"https://data.sec.gov/submissions/CIK{UNLISTED_CIK}.json"])

    def test_details_enrich_metadata_and_select_latest_unamended_10k(self):
        details = self.sec.company_details(NAME)
        self.assertEqual(details, {
            "ticker": "ORNT", "cik": CIK, "name": NAME, "exchanges": ["NYSE"],
            "industry": "Synthetic test manufacturing", "sic": "3999",
            "description": "Mock SEC metadata only", "entity_type": "operating", "fiscal_year_end": "1231",
            "address": "1 Test Street, Suite 2, Test City, Test State, 00000",
            "source_url": SUBMISSIONS_URL, "offline": False, "filing_available": True,
            "latest_10k": {"form": "10-K", "accession": ACCESSION, "report_date": "2024-12-31",
                           "filing_date": "2025-02-01", "url": FILING_URL},
        })
        self.assertEqual(self.source.urls, [CATALOG_URL, SUBMISSIONS_URL])

    def test_submissions_preserve_selected_ticker_alias(self):
        for ticker in ("ORNT", "ORNT.A", "ORNT.B"):
            with self.subTest(ticker=ticker):
                self.assertEqual(self.sec.company_details(ticker)["ticker"], ticker)
        self.assertEqual(self.sec.company_details(CIK)["ticker"], "ORNT")

    def test_optional_metadata_absence_is_supported(self):
        minimal = {"cik": int(CIK), "name": NAME, "filings": {"recent": annual_batch()}}
        self.source.routes[SUBMISSIONS_URL] = minimal
        details = self.sec.company_details(CIK)
        self.assertEqual(details["ticker"], f"CIK{CIK}")
        self.assertEqual(details["exchanges"], [])
        for field in ("address", "industry", "sic", "description", "entity_type", "fiscal_year_end"):
            self.assertEqual(details[field], "")
        self.assertTrue(details["filing_available"])

    def test_demo_remains_offline_for_search_details_filing_and_facts(self):
        self.assertEqual(self.sec.search_companies("demo"), [COMPANIES[0]])
        details = self.sec.company_details("DEMO")
        self.assertTrue(details["offline"])
        self.assertEqual(details["latest_10k"]["form"], "10-K")
        filing = self.sec.load("DEMO")
        self.assertTrue(filing.offline)
        self.assertEqual(filing.html, self.source.html)
        self.assertEqual(filing.company_facts, self.source.facts)
        self.assertEqual(self.source.urls, [])

    def test_load_uses_real_retrieval_with_mocked_demo_financial_evidence(self):
        filing = self.sec.load("CIK1234567")
        self.assertFalse(filing.offline)
        self.assertEqual(filing.company["name"], NAME)
        self.assertEqual(filing.url, FILING_URL)
        self.assertEqual(filing.html, self.source.html)
        self.assertEqual(filing.company_facts, self.source.facts)
        self.assertEqual(self.source.urls, [SUBMISSIONS_URL, FILING_URL, FACTS_URL])

    def test_recent_10k_does_not_fetch_archive(self):
        archive = self.source.archive(f"CIK{CIK}-submissions-001.json", {})
        self.assertEqual(self.sec.company_details(CIK)["latest_10k"]["accession"], ACCESSION)
        self.assertNotIn(archive, self.source.urls)

    def test_archived_10k_is_found_after_recent_and_first_archive_have_none(self):
        self.source.metadata["filings"]["recent"] = filing_batch([
            ("8-K", ACCESSION, "2024-12-31", "2025-06-01", "current.htm")])
        first = self.source.archive(f"CIK{CIK}-submissions-001.json", filing_batch([
            ("20-F", ACCESSION, "2024-12-31", "2025-05-01", "foreign.htm")]))
        second = self.source.archive(f"CIK{CIK}-submissions-002.json", annual_batch())
        third = self.source.archive(f"CIK{CIK}-submissions-003.json", {})
        filing = self.sec.retrieve_filing(CIK)
        self.assertEqual(filing.accession, ACCESSION)
        self.assertEqual(filing.html, self.source.html)
        self.assertEqual(self.source.urls, [SUBMISSIONS_URL, first, second, FILING_URL])
        self.assertNotIn(third, self.source.urls)

    def test_no_10k_is_not_silently_replaced_by_20f_40f_or_amendment(self):
        details = self.sec.company_details(NO_10K_CIK)
        self.assertFalse(details["filing_available"])
        self.assertIsNone(details["latest_10k"])
        with self.assertRaisesRegex(ValueError, "20-F or 40-F is not a 10-K"):
            self.sec.retrieve_filing(NO_10K_CIK)
        self.assertTrue(all("/Archives/" not in url for url in self.source.urls))

    def test_wrong_submissions_cik_rejected_before_filing_or_facts(self):
        self.source.metadata["cik"] = 1234568
        with self.assertRaisesRegex(ValueError, "different company identifier"):
            self.sec.retrieve_filing(CIK)
        self.assertEqual(self.source.urls, [SUBMISSIONS_URL])

    def test_404_is_notfound_for_catalog_submissions_filing_and_facts(self):
        for url, method, identifier in (
            (CATALOG_URL, "search_companies", "orion"),
            (SUBMISSIONS_URL, "company_details", CIK),
            (FILING_URL, "retrieve_filing", CIK),
            (FACTS_URL, "lookup_company_facts", CIK),
        ):
            with self.subTest(url=url):
                source = MockSECSource()
                source.fail(url, 404)
                client = SecClient(transport=source.transport)
                with self.assertRaisesRegex(ValueError, "not found|No SEC filer found"):
                    getattr(client, method)(identifier)

    def test_403_429_and_connection_errors_are_sec_unavailable_not_notfound(self):
        for kind in (403, 429, "timeout", "connection"):
            for url, method, identifier in (
                (CATALOG_URL, "search_companies", "orion"),
                (SUBMISSIONS_URL, "company_details", CIK),
                (FILING_URL, "retrieve_filing", CIK),
                (FACTS_URL, "lookup_company_facts", CIK),
            ):
                with self.subTest(kind=kind, url=url):
                    source = MockSECSource()
                    source.fail(url, kind)
                    client = SecClient(transport=source.transport)
                    with self.assertRaises(SecUnavailableError):
                        getattr(client, method)(identifier)

    def test_invalid_json_is_sec_unavailable_for_catalog_submissions_facts_and_archive(self):
        for url, method, identifier in (
            (CATALOG_URL, "search_companies", "orion"),
            (SUBMISSIONS_URL, "company_details", CIK),
            (FACTS_URL, "lookup_company_facts", CIK),
            (f"https://data.sec.gov/submissions/CIK{CIK}-submissions-001.json", "company_details", CIK),
        ):
            with self.subTest(url=url):
                source = MockSECSource()
                source.metadata["filings"]["recent"] = filing_batch([])
                source.archive(f"CIK{CIK}-submissions-001.json", {})
                source.fail(url, "invalid_json")
                with self.assertRaisesRegex(SecUnavailableError, "unreadable response"):
                    getattr(SecClient(transport=source.transport), method)(identifier)

    def test_unsafe_identifiers_rejected_by_all_public_entrypoints_without_network(self):
        for identifier in ("", "   ", "https://www.sec.gov/Archives/x", "http://attacker.invalid", "file:secret",
                           "../private", "ORNT/../../x", "ORNT\\x", "OR\x00NT", "OR\nNT", "OR\tNT",
                           "OR\rNT", "OR\x1fNT", "x" * 161):
            for method in ("company", "search_companies", "company_details", "retrieve_filing",
                           "lookup_company_facts", "load"):
                with self.subTest(identifier=repr(identifier), method=method), self.assertRaises(ValueError):
                    getattr(self.sec, method)(identifier)
        self.assertEqual(self.source.urls, [])

    def test_edge_control_characters_are_not_trimmed_into_valid_identifiers(self):
        for identifier in ("\nDEMO", "DEMO\r", "\tDEMO", "DEMO\x1f", "DE\x7fMO"):
            with self.subTest(identifier=repr(identifier)), self.assertRaises(ValueError):
                self.sec.company(identifier)
        self.assertEqual(self.source.urls, [])

    def test_malformed_filing_document_paths_rejected_without_document_request(self):
        for document in ("../secret.htm", "x/secret.htm", "x\\secret.htm", "https://attacker.invalid/x",
                         "//attacker.invalid/x", "x%2fsecret.htm", "x?query=1", "x#fragment", "x\n.htm", "x..htm"):
            with self.subTest(document=repr(document)):
                source = MockSECSource()
                source.metadata["filings"]["recent"]["primaryDocument"][-1] = document
                with self.assertRaisesRegex(ValueError, "invalid filing document path"):
                    SecClient(transport=source.transport).retrieve_filing(CIK)
                self.assertEqual(source.urls, [SUBMISSIONS_URL])

    def test_malformed_accession_rejected_before_fetch(self):
        for accession in ("../123", "0001234567-25-1", "000123456725000001", "0001234567-25-000001/extra"):
            with self.subTest(accession=accession):
                source = MockSECSource()
                source.metadata["filings"]["recent"]["accessionNumber"][-1] = accession
                with self.assertRaisesRegex(ValueError, "invalid filing document path"):
                    SecClient(transport=source.transport).company_details(CIK)
                self.assertEqual(source.urls, [SUBMISSIONS_URL])

    def test_malformed_historical_paths_cannot_escape_verified_cik(self):
        for filename in ("../history.json", "https://attacker.invalid/history.json",
                         f"CIK{CIK}-submissions-001.json/extra", "CIK0001234568-submissions-001.json",
                         f"CIK{CIK}-submissions-x.json", f"CIK{CIK}-submissions-001.json\n"):
            with self.subTest(filename=repr(filename)):
                source = MockSECSource()
                source.metadata["filings"]["recent"] = filing_batch([])
                source.metadata["filings"]["files"] = [{"name": filename}]
                with self.assertRaisesRegex(ValueError, "invalid historical submissions path"):
                    SecClient(transport=source.transport).company_details(CIK)
                self.assertEqual(source.urls, [SUBMISSIONS_URL])

    def test_malformed_catalog_schema_is_a_controlled_sec_error(self):
        self.source.routes[CATALOG_URL] = {}
        with self.assertRaises(SecUnavailableError):
            self.sec.search_companies("orion")

    def test_malformed_submissions_schema_is_a_controlled_sec_error(self):
        self.source.routes[SUBMISSIONS_URL] = {"cik": int(CIK)}
        with self.assertRaises(SecUnavailableError):
            self.sec.company_details(CIK)

    def test_misaligned_filing_arrays_are_a_controlled_sec_error(self):
        self.source.metadata["filings"]["recent"]["primaryDocument"] = []
        with self.assertRaises(SecUnavailableError):
            self.sec.company_details(CIK)

    def test_mcp_functions_accept_exact_names_and_cik_and_keep_metadata(self):
        mcp = InProcessMCP(self.sec)
        filing = mcp.call("retrieve_10k_filing", {"ticker": NAME})
        facts = mcp.call("lookup_xbrl_company_facts", {"ticker": CIK})
        self.assertEqual(filing["company"]["cik"], CIK)
        self.assertEqual(filing["company"]["industry"], "Synthetic test manufacturing")
        self.assertEqual(filing["html"], self.source.html)
        self.assertFalse(filing["offline"])
        self.assertEqual(facts, {"ticker": CIK, "company_facts": self.source.facts})


class RunRequestTests(unittest.TestCase):
    def test_identifier_length160_and_default_thresholds(self):
        for identifier in (NAME, "CIK 1234567", "x" * 160):
            request = RunRequest(ticker=identifier)
            self.assertEqual(request.ticker, identifier)
            self.assertEqual(request.confidence_threshold, 0.85)
            self.assertEqual(request.variance_threshold, 0.50)
        for identifier in ("", "x" * 161):
            with self.subTest(length=len(identifier)), self.assertRaises(ValidationError):
                RunRequest(ticker=identifier)

    def test_existing_threshold_bounds_are_unchanged(self):
        for confidence in (0, 1):
            for variance in (0.05, 5):
                self.assertEqual(RunRequest(ticker="DEMO", confidence_threshold=confidence,
                                            variance_threshold=variance).variance_threshold, variance)
        for values in ({"confidence_threshold": -0.01}, {"confidence_threshold": 1.01},
                       {"variance_threshold": 0.049}, {"variance_threshold": 5.01}):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                RunRequest(ticker="DEMO", **values)


class SecWorkflowTests(OfflineTests):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory(prefix="finflow-sec-workflow-test-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.db = Database(str(self.directory / "audits.sqlite"))
        self.checkpoint_path = str(self.directory / "agent.sqlite")
        self.mcp = InProcessMCP(self.sec)
        self.workflow = AuditWorkflow(self.db, self.sec, checkpoint_path=self.checkpoint_path, mcp_client=self.mcp)
        self.addCleanup(lambda: self.workflow.close())
        self.assertFalse(self.workflow.explainer.configured)
        self.assertFalse(self.workflow.assistant.configured)

    def snapshot(self):
        return database_dump(self.db.path), database_dump(self.checkpoint_path)

    def checkpoint(self, run):
        return self.workflow.agent.graph.get_state(self.workflow.agent._config(run["id"])).values

    def assert_demo_metrics(self, run):
        self.assertEqual(run["status"], "awaiting_review", run.get("error"))
        metrics = {metric["key"]: metric for metric in run["state"]["metrics"]}
        self.assertEqual({key: metric["value"] for key, metric in metrics.items()}, {
            "revenue": 100_000_000, "net_income": 10_000_000, "total_assets": 80_000_000,
            "total_liabilities": 35_000_000, "shareholders_equity": 50_000_000,
            "operating_cash_flow": 12_000_000, "cash_and_equivalents": 8_000_000, "diluted_eps": 4.0,
        })
        self.assertEqual({key for key, metric in metrics.items() if metric["status"] == "pending"},
                         {"total_assets", "total_liabilities", "shareholders_equity", "cash_and_equivalents"})
        self.assertEqual(len(self.db.get_reviews(run["id"])), 4)
        for key, metric in metrics.items():
            self.assertEqual(metric["unit"], "USD/shares" if key == "diluted_eps" else "USD")
            self.assertEqual(metric["period"], "2024-12-31")
            self.assertTrue(metric["source_snippet"])
            self.assertEqual(metric["explanation"]["generated_by"], "rule_based_fallback")
            self.assertEqual(next(check for check in metric["validations"] if check["rule"] == "year_on_year")
                             ["details"]["threshold"], 0.50)
        accounting = next(check for check in metrics["total_assets"]["validations"]
                          if check["rule"] == "accounting_equation")
        self.assertEqual(accounting["status"], "failed")
        self.assertEqual(accounting["details"], {"difference": -5_000_000, "tolerance": 80_000})
        segment = next(check for check in metrics["revenue"]["validations"] if check["rule"] == "segment_total")
        self.assertEqual(segment["status"], "passed")
        self.assertEqual(segment["details"]["segment_total"], 100_000_000)

    def test_name_start_verifies_details_before_create_and_passes_numeric_cik_to_pipeline(self):
        real_create = self.db.create_run
        observations = []

        def create(*args):
            observations.append((list(self.source.urls), copy.deepcopy(args[3]), list(self.mcp.calls)))
            return real_create(*args)

        with patch.object(self.db, "create_run", side_effect=create):
            run = self.workflow.start(NAME)
        self.assert_demo_metrics(run)
        self.assertEqual(observations[0][0], [CATALOG_URL, SUBMISSIONS_URL])
        self.assertTrue(observations[0][1]["company"]["filing_available"])
        self.assertEqual(observations[0][2], [])
        self.assertEqual(self.mcp.calls, [("retrieve_10k_filing", {"ticker": CIK}),
                                         ("lookup_xbrl_company_facts", {"ticker": CIK})])
        self.assertEqual(self.checkpoint(run)["ticker"], CIK)
        self.assertEqual(run["ticker"], "ORNT")
        self.assertEqual(run["company_name"], NAME)
        self.assertEqual(run["state"]["company"], self.checkpoint(run)["filing"]["company"])
        self.assertEqual(run["state"]["confidence_threshold"], 0.85)
        self.assertEqual(run["state"]["variance_threshold"], 0.50)

    def test_cik_only_unlisted_filer_can_start_without_catalog(self):
        url = f"https://data.sec.gov/submissions/CIK{UNLISTED_CIK}.json"
        filing_url = FILING_URL.replace("/1234567/", "/7654321/")
        self.source.routes[filing_url] = self.source.html
        self.source.routes[f"https://data.sec.gov/api/xbrl/companyfacts/CIK{UNLISTED_CIK}.json"] = self.source.facts
        run = self.workflow.start("CIK7654321")
        self.assert_demo_metrics(run)
        self.assertEqual(run["ticker"], f"CIK{UNLISTED_CIK}")
        self.assertEqual(run["state"]["company"]["name"], "Unlisted Test Filer LLC")
        self.assertEqual(self.source.urls[:2], [url, url])
        self.assertNotIn(CATALOG_URL, self.source.urls)
        self.assertTrue(all(arguments == {"ticker": UNLISTED_CIK} for _, arguments in self.mcp.calls))

    def test_sync_uses_filing_company_not_stale_preflight_metadata(self):
        real_details = self.sec.company_details
        with patch.object(self.sec, "company_details", side_effect=lambda identifier: {
            **real_details(identifier), "description": "Preflight-only description",
        }):
            run = self.workflow.start("ORNT.A")
        self.assert_demo_metrics(run)
        self.assertEqual(run["state"]["company"]["description"], "Mock SEC metadata only")
        self.assertEqual(run["state"]["company"], self.checkpoint(run)["filing"]["company"])
        self.assertEqual(run["ticker"], "ORNT")

    def test_demo_financial_rules_and_pipeline_are_unchanged(self):
        demo = self.workflow.start("DEMO")
        self.assert_demo_metrics(demo)
        self.assertEqual(self.source.urls, [])
        self.assertEqual(self.mcp.calls, [("retrieve_10k_filing", {"ticker": "DEMO"}),
                                         ("lookup_xbrl_company_facts", {"ticker": "DEMO"})])
        self.assertTrue(demo["state"]["filing"]["offline"])
        live = self.workflow.start(CIK)
        self.assert_demo_metrics(live)
        for original, current in zip(demo["state"]["metrics"], live["state"]["metrics"]):
            self.assertEqual({key: value for key, value in original.items() if key != "source_url"},
                             {key: value for key, value in current.items() if key != "source_url"})

    def test_custom_thresholds_propagate_without_changing_eight_extracted_values(self):
        run = self.workflow.start(CIK, confidence_threshold=0.99, variance_threshold=0.05)
        self.assertEqual(run["status"], "awaiting_review", run.get("error"))
        self.assertEqual(len(run["state"]["metrics"]), 8)
        self.assertEqual(len(self.db.get_reviews(run["id"])), 8)
        self.assertEqual(run["state"]["confidence_threshold"], 0.99)
        self.assertEqual(run["state"]["variance_threshold"], 0.05)
        state = self.checkpoint(run)
        self.assertEqual(state["confidence_threshold"], 0.99)
        self.assertEqual(state["variance_threshold"], 0.05)
        self.assertTrue(all(next(check for check in metric["validations"] if check["rule"] == "year_on_year")
                            ["details"]["threshold"] == 0.05 for metric in run["state"]["metrics"]))

    def test_invalid_unknown_ambiguous_no10k_and_missing_cik_create_no_run_or_checkpoint(self):
        self.source.fail("https://data.sec.gov/submissions/CIK0009999999.json", 404)
        for identifier in ("https://attacker.invalid/x", "OR\nNT", "Orion", "Shared Test Holdings",
                           NO_10K_CIK, "CIK9999999"):
            with self.subTest(identifier=identifier):
                before = self.snapshot()
                with self.assertRaises(ValueError):
                    self.workflow.start(identifier)
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.mcp.calls, [])

    def test_preflight_sec_connection_failures_do_not_persist_failed_runs(self):
        for kind in (403, 429, "timeout", "connection", "invalid_json"):
            with self.subTest(kind=kind):
                self.source.fail(SUBMISSIONS_URL, kind)
                before = self.snapshot()
                with self.assertRaises(SecUnavailableError):
                    self.workflow.start(CIK)
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.mcp.calls, [])

    def test_company_metadata_survives_correction_checkpoint_restart_without_refetch(self):
        run = self.workflow.start(NAME)
        self.assert_demo_metrics(run)
        metadata = copy.deepcopy(run["state"]["company"])
        explanations = {metric["key"]: metric["explanation"] for metric in run["state"]["metrics"]}
        original_urls, original_calls = list(self.source.urls), list(self.mcp.calls)
        review = next(item for item in self.db.get_reviews(run["id"]) if item["metric_key"] == "total_liabilities")
        run = self.workflow.submit_decision(run["id"], review["id"], ReviewRequest(
            decision="correct", corrected_value=30_000_000, reviewer="temporary-sec-reviewer"))
        self.assertEqual(run["state"]["company"], metadata)
        self.assertEqual(self.checkpoint(run)["filing"]["company"], metadata)
        self.workflow.close()
        restarted_source = MockSECSource()
        restarted_sec = SecClient(transport=restarted_source.transport)
        restarted_mcp = InProcessMCP(restarted_sec)
        self.db = Database(self.db.path)
        self.workflow = AuditWorkflow(self.db, restarted_sec, checkpoint_path=self.checkpoint_path,
                                      mcp_client=restarted_mcp)
        persisted = self.db.get_run(run["id"])
        self.assertEqual(persisted["state"]["company"], metadata)
        self.assertEqual(self.checkpoint(persisted)["filing"]["company"], metadata)
        for review in self.db.get_reviews(run["id"]):
            if review["status"] == "pending":
                run = self.workflow.submit_decision(run["id"], review["id"], ReviewRequest(
                    decision="approve", reviewer="temporary-restart-reviewer"))
                self.assertEqual(run["state"]["company"], metadata)
                self.assertEqual(self.checkpoint(run)["filing"]["company"], metadata)
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["state"]["report"]["accuracy_percentage"], 100.0)
        self.assertEqual(run["state"]["report"]["automatically_approved"], 4)
        self.assertEqual(run["state"]["report"]["completed_reviews"], 4)
        self.assertEqual({metric["key"]: metric["explanation"] for metric in run["state"]["metrics"]}, explanations)
        self.assertEqual(self.source.urls, original_urls)
        self.assertEqual(self.mcp.calls, original_calls)
        self.assertEqual(restarted_source.urls, [])
        self.assertEqual(restarted_mcp.calls, [])
        self.assertEqual(len(self.db.get_audit(run["id"])), 4)
        self.assertTrue(self.db.verify_audit(run["id"])["valid"])
        with self.db.connect() as connection:
            states = [json.loads(row[0]) for row in connection.execute(
                "SELECT state_json FROM checkpoints WHERE run_id=?", (run["id"],))]
        self.assertTrue(states)
        self.assertTrue(all(state["company"] == metadata for state in states))


class SecAPITests(OfflineTests):
    @classmethod
    def setUpClass(cls):
        # Importing main creates stores; only do so with all paths redirected first.
        if "app.main" in sys.modules:
            raise RuntimeError("SEC API tests require a fresh app.main import with temporary stores")
        temporary = tempfile.TemporaryDirectory(prefix="finflow-sec-api-test-")
        cls.addClassCleanup(temporary.cleanup)
        cls.directory = Path(temporary.name)
        cls.audit_path = str(cls.directory / "audits.sqlite")
        cls.agent_path = str(cls.directory / "agent.sqlite")
        cls.auth_path = str(cls.directory / "auth.sqlite")
        environment = patch.dict(os.environ, {
            "AUDITOR_DB_PATH": cls.audit_path, "AUDITOR_AGENT_DB_PATH": cls.agent_path,
            "FINFLOW_AUTH_DB_PATH": cls.auth_path, "LLM_API_KEY": "", "OPENAI_API_KEY": "",
            "MPLCONFIGDIR": str(cls.directory),
        })
        environment.start()
        cls.addClassCleanup(environment.stop)
        cls.main = importlib.import_module("app.main")
        cls.addClassCleanup(cls.remove_main_import)
        cls.addClassCleanup(cls.main.workflow.close)
        if cls.main.database.path != cls.audit_path or cls.main.auth_store.path != cls.auth_path:
            raise AssertionError("SEC tests escaped temporary application stores")
        checkpoint = cls.main.workflow.agent._checkpoint_connection.execute("PRAGMA database_list").fetchone()[2]
        if Path(checkpoint).resolve() != Path(cls.agent_path).resolve():
            raise AssertionError("SEC tests escaped temporary checkpoint store")
        if cls.main.workflow.explainer.configured or cls.main.workflow.assistant.configured:
            raise AssertionError("SEC tests must not enable LLM providers")
        cls.username = "temporary-sec-api-owner"
        cls.password = secrets.token_urlsafe(24)
        cls.main.auth_store.initialize_owner(cls.username, cls.password)

    @classmethod
    def remove_main_import(cls):
        if sys.modules.get("app.main") is cls.main:
            sys.modules.pop("app.main")
            package = sys.modules.get("app")
            if getattr(package, "main", None) is cls.main:
                delattr(package, "main")

    def setUp(self):
        super().setUp()
        with self.main.auth_store.connect() as connection:
            connection.execute("DELETE FROM sessions")
        self.mcp = InProcessMCP(self.sec)
        for attribute, value in (("sec", self.sec), ("mcp", self.mcp)):
            replacement = patch.object(self.main.workflow, attribute, value)
            replacement.start()
            self.addCleanup(replacement.stop)
        # Deliberately not a context manager: do not close the shared workflow per test.
        self.client = TestClient(self.main.app, base_url="http://localhost", follow_redirects=False,
                                 client=("127.0.0.1", 50_000))
        self.addCleanup(self.client.close)

    def assert_response(self, response, status):
        self.assertEqual(response.status_code, status, response.text[:500])
        self.assertEqual(response.headers.get("cache-control"), "no-store")
        return response

    def login(self):
        self.assert_response(self.client.post("/api/auth/login", json={
            "username": self.username, "password": self.password,
        }, headers={"Origin": "http://localhost"}), 200)
        self.assertTrue(self.client.cookies.get(COOKIE_NAME))
        session = self.assert_response(self.client.get("/api/auth/session"), 200).json()
        return {"Origin": "http://localhost", "X-CSRF-Token": session["csrf_token"]}

    def snapshot(self):
        return database_dump(self.audit_path), database_dump(self.agent_path)

    def test_search_and_details_require_authentication_before_any_sec_request(self):
        before = self.snapshot()
        for path in ("/api/companies/search?q=orion", f"/api/companies/{CIK}", "/api/companies/DEMO"):
            with self.subTest(path=path):
                response = self.assert_response(self.client.get(path), 401)
                self.assertEqual(response.json(), {"detail": "Please sign in to FinFlow."})
                self.assert_response(self.client.get(path, headers={"Cookie": f"{COOKIE_NAME}=forged"}), 401)
        self.assert_response(self.client.post("/api/runs", json={"ticker": NAME},
                                              headers={"Origin": "http://localhost"}), 401)
        self.assertEqual(self.source.urls, [])
        self.assertEqual(self.mcp.calls, [])
        self.assertEqual(self.snapshot(), before)

    def test_authenticated_get_search_details_and_cik_lookup_do_not_mutate_financial_stores(self):
        self.login()
        before = self.snapshot(), database_dump(self.auth_path)
        results = self.assert_response(self.client.get("/api/companies/search", params={"q": "orion"}), 200).json()
        self.assertEqual({company["ticker"] for company in results}, {"ORNT", "ORNT.A", "ORNT.B"})
        details = self.assert_response(self.client.get("/api/companies/" + quote(NAME, safe="")), 200).json()
        self.assertEqual(details["source_url"], SUBMISSIONS_URL)
        self.assertEqual(details["latest_10k"]["url"], FILING_URL)
        unlisted = self.assert_response(self.client.get("/api/companies/search", params={"q": UNLISTED_CIK}), 200).json()
        self.assertEqual(unlisted[0]["name"], "Unlisted Test Filer LLC")
        self.assertEqual((self.snapshot(), database_dump(self.auth_path)), before)
        self.assertEqual(self.mcp.calls, [])
        self.assertNotIn(FILING_URL, self.source.urls)
        self.assertNotIn(FACTS_URL, self.source.urls)

    def test_authenticated_search_keeps_ranking_limit_and_empty_results(self):
        self.login()
        results = self.assert_response(self.client.get("/api/companies/search", params={"q": "orbit"}), 200).json()
        self.assertEqual(len(results), 20)
        self.assertEqual([company["ticker"] for company in results[:2]], ["ORBIT", "EXACT"])
        self.assertEqual(self.assert_response(self.client.get("/api/companies/search",
                                                             params={"q": "no match"}), 200).json(), [])

    def test_direct_post_exact_name_creates_verified_live_run_with_demo_values(self):
        headers = self.login()
        run = self.assert_response(self.client.post("/api/runs", json={"ticker": NAME}, headers=headers), 200).json()
        self.assertEqual(run["status"], "awaiting_review", run.get("error"))
        self.assertEqual(run["ticker"], "ORNT")
        self.assertEqual(run["company_name"], NAME)
        self.assertEqual(run["state"]["company"]["cik"], CIK)
        self.assertEqual(run["state"]["company"]["industry"], "Synthetic test manufacturing")
        self.assertEqual(run["state"]["filing"]["url"], FILING_URL)
        self.assertFalse(run["state"]["filing"]["offline"])
        self.assertEqual(len(run["state"]["metrics"]), 8)
        self.assertEqual(run["state"]["metrics"][0]["value"], 100_000_000)
        self.assertEqual(len(run["reviews"]), 4)
        self.assertEqual(self.mcp.calls, [("retrieve_10k_filing", {"ticker": CIK}),
                                         ("lookup_xbrl_company_facts", {"ticker": CIK})])

    def test_direct_post_numeric_and_prefixed_cik_skip_catalog(self):
        headers = self.login()
        for identifier in ("1234567", "CIK 0001234567"):
            with self.subTest(identifier=identifier):
                run = self.assert_response(self.client.post("/api/runs", json={"ticker": identifier},
                                                           headers=headers), 200).json()
                self.assertEqual(run["status"], "awaiting_review", run.get("error"))
                self.assertEqual(run["state"]["company"]["cik"], CIK)
        self.assertNotIn(CATALOG_URL, self.source.urls)
        self.assertTrue(all(arguments == {"ticker": CIK} for _, arguments in self.mcp.calls))

    def test_direct_post_accepts160_character_exact_name(self):
        headers = self.login()
        name = "Long Test Company " + "x" * (160 - len("Long Test Company "))
        self.source.catalog["data"].append(["NYSE", name, int(CIK), "ORNT"])
        response = self.client.post("/api/runs", json={"ticker": name}, headers=headers)
        run = self.assert_response(response, 200).json()
        self.assertEqual(run["status"], "awaiting_review", run.get("error"))
        self.assertEqual(run["state"]["company"]["cik"], CIK)

    def test_post_keeps_existing_origin_csrf_and_json_middleware(self):
        headers = self.login()
        before = self.snapshot(), database_dump(self.auth_path)
        for supplied in ({}, {"Origin": "http://attacker.invalid", "X-CSRF-Token": headers["X-CSRF-Token"]},
                         {"Origin": "http://localhost"}, {"Origin": "http://localhost", "X-CSRF-Token": "wrong"}):
            with self.subTest(headers=supplied):
                self.assert_response(self.client.post("/api/runs", json={"ticker": NAME}, headers=supplied), 403)
        self.assert_response(self.client.post("/api/runs", content='{"ticker":"CIK1234567"}', headers={
            **headers, "Content-Type": "text/plain",
        }), 415)
        self.assertEqual((self.snapshot(), database_dump(self.auth_path)), before)
        self.assertEqual(self.source.urls, [])
        self.assertEqual(self.mcp.calls, [])

    def test_invalid_models_are422_without_network_or_financial_mutation(self):
        headers = self.login()
        before = self.snapshot()
        for payload in ({}, {"ticker": ""}, {"ticker": "x" * 161}, {"ticker": None}, {"ticker": [NAME]},
                        {"ticker": NAME, "confidence_threshold": 1.1}, {"ticker": NAME, "variance_threshold": 0.01}):
            with self.subTest(payload=payload):
                self.assert_response(self.client.post("/api/runs", json=payload, headers=headers), 422)
        for params in ({}, {"q": ""}, {"q": "x" * 161}):
            with self.subTest(params=params):
                self.assert_response(self.client.get("/api/companies/search", params=params), 422)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.source.urls, [])

    def test_unsafe_query_details_and_post_identifiers_are400_without_network(self):
        headers = self.login()
        before = self.snapshot()
        for identifier in ("https://attacker.invalid/x", "../secret", "ORNT\\secret", "OR\x00NT", "OR\nNT"):
            with self.subTest(identifier=repr(identifier)):
                self.assert_response(self.client.get("/api/companies/search", params={"q": identifier}), 400)
                self.assert_response(self.client.post("/api/runs", json={"ticker": identifier}, headers=headers), 400)
        for identifier in ("file:secret", "OR\x00NT", "OR\nNT"):
            with self.subTest(details=repr(identifier)):
                self.assert_response(self.client.get("/api/companies/" + quote(identifier, safe="")), 400)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.source.urls, [])
        self.assertEqual(self.mcp.calls, [])

    def test_unknown_partial_and_ambiguous_names_are400_without_creating_runs(self):
        headers = self.login()
        before = self.snapshot()
        for identifier in ("No Such Test Filer", "Orion", "Shared Test Holdings"):
            with self.subTest(identifier=identifier):
                self.assert_response(self.client.get("/api/companies/" + quote(identifier, safe="")), 400)
                self.assert_response(self.client.post("/api/runs", json={"ticker": identifier}, headers=headers), 400)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.mcp.calls, [])

    def test_404_cik_is400_for_search_details_and_start_and_does_not_create_run(self):
        headers = self.login()
        before = self.snapshot()
        self.source.fail(SUBMISSIONS_URL, 404)
        responses = [self.client.get("/api/companies/search", params={"q": CIK}),
                     self.client.get(f"/api/companies/{CIK}"),
                     self.client.post("/api/runs", json={"ticker": CIK}, headers=headers)]
        for response in responses:
            self.assert_response(response, 400)
            self.assertIn("No SEC filer found", response.json()["detail"])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.mcp.calls, [])

    def test_sec403_429_timeouts_connections_and_invalid_json_map_to502(self):
        headers = self.login()
        before = self.snapshot()
        for kind in (403, 429, "timeout", "connection", "invalid_json"):
            with self.subTest(kind=kind):
                self.source.fail(SUBMISSIONS_URL, kind)
                self.source.fail(CATALOG_URL, kind)
                for response in (
                    self.client.get("/api/companies/search", params={"q": "orion"}),
                    self.client.get(f"/api/companies/{CIK}"),
                    self.client.post("/api/runs", json={"ticker": CIK}, headers=headers),
                ):
                    self.assert_response(response, 502)
                    self.assertIn("SEC EDGAR", response.json()["detail"])
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.mcp.calls, [])

    def test_no10k_details_expose_unavailability_but_post_is400_not_foreign_filing(self):
        headers = self.login()
        before = self.snapshot()
        details = self.assert_response(self.client.get(f"/api/companies/{NO_10K_CIK}"), 200).json()
        self.assertFalse(details["filing_available"])
        self.assertIsNone(details["latest_10k"])
        response = self.assert_response(self.client.post("/api/runs", json={"ticker": NO_10K_CIK},
                                                       headers=headers), 400)
        self.assertIn("20-F or 40-F is not a 10-K", response.json()["detail"])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.mcp.calls, [])
        self.assertTrue(all("/Archives/" not in url for url in self.source.urls))

    def test_invalid_sec_document_and_historical_paths_are400_without_run(self):
        headers = self.login()
        before = self.snapshot()
        self.source.metadata["filings"]["recent"]["primaryDocument"][-1] = "../secret.htm"
        self.assert_response(self.client.get(f"/api/companies/{CIK}"), 400)
        self.assert_response(self.client.post("/api/runs", json={"ticker": CIK}, headers=headers), 400)
        self.source.metadata["filings"]["recent"] = filing_batch([])
        self.source.metadata["filings"]["files"] = [{"name": "../history.json"}]
        self.assert_response(self.client.get(f"/api/companies/{CIK}"), 400)
        self.assert_response(self.client.post("/api/runs", json={"ticker": CIK}, headers=headers), 400)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.mcp.calls, [])
        self.assertTrue(all(url == SUBMISSIONS_URL for url in self.source.urls))

    def test_malformed_sec_schema_maps_to502_not_internal_server_error(self):
        self.login()
        before = self.snapshot()
        self.source.routes[CATALOG_URL] = {}
        client = TestClient(self.main.app, base_url="http://localhost", raise_server_exceptions=False,
                            client=("127.0.0.1", 50_000))
        self.addCleanup(client.close)
        client.cookies.update(self.client.cookies)
        self.assert_response(client.get("/api/companies/search", params={"q": "orion"}), 502)
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
