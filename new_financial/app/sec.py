import json
import re
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from time import monotonic

import httpx


COMPANIES = [
    {"ticker": "DEMO", "cik": "0000000000", "name": "Demo Manufacturing Corp."},
    {"ticker": "AAPL", "cik": "0000320193", "name": "Apple Inc."},
    {"ticker": "MSFT", "cik": "0000789019", "name": "Microsoft Corp."},
    {"ticker": "KO", "cik": "0000021344", "name": "The Coca-Cola Co."},
    {"ticker": "TSLA", "cik": "0001318605", "name": "Tesla, Inc."},
    {"ticker": "NKE", "cik": "0000320187", "name": "NIKE, Inc."},
]


class SecUnavailableError(RuntimeError):
    pass


@dataclass
class Filing:
    company: dict
    accession: str
    report_date: str
    url: str
    html: str
    company_facts: dict
    offline: bool = False


class SecClient:
    def __init__(self, user_agent: str | None = None, transport=None):
        import os

        self.user_agent = user_agent or os.getenv(
            "SEC_USER_AGENT", "FinancialFilingAuditor/1.0 student-hackathon contact@example.com"
        )
        self.headers = {"User-Agent": self.user_agent, "Accept-Encoding": "gzip, deflate"}
        self.transport = transport
        self._catalog = None
        self._catalog_loaded = 0
        self._catalog_lock = Lock()

    def _client(self):
        return httpx.Client(headers=self.headers, timeout=45, follow_redirects=True, transport=self.transport)

    @staticmethod
    def _identifier(value: str) -> str:
        value = value.strip()
        if not value or len(value) > 160 or re.search(r"[\x00-\x1f/\\]", value) or ":" in value:
            raise ValueError("Enter a company name, ticker, or a numeric SEC CIK, not a URL.")
        return value

    @staticmethod
    def _cik(value: str) -> str | None:
        match = re.fullmatch(r"(?:CIK\s*)?(\d{1,10})", value, re.IGNORECASE)
        if match:
            if int(match[1]) == 0:
                raise ValueError("Enter a valid SEC CIK, or choose DEMO for the offline filing.")
            return match[1].zfill(10)
        return None

    def _companies(self) -> list[dict]:
        with self._catalog_lock:
            if self._catalog is None or monotonic() - self._catalog_loaded >= 3600:
                with self._client() as client:
                    data = self._json(client, "https://www.sec.gov/files/company_tickers_exchange.json")
                positions = {name: index for index, name in enumerate(data["fields"])}
                self._catalog = [
                    {
                        "ticker": row[positions["ticker"]],
                        "cik": str(row[positions["cik"]]).zfill(10),
                        "name": row[positions["name"]],
                        "exchange": row[positions["exchange"]],
                    }
                    for row in data["data"]
                ]
                self._catalog_loaded = monotonic()
            return self._catalog

    def search_companies(self, query: str) -> list[dict]:
        query = self._identifier(query)
        if query.upper() == "DEMO":
            return [dict(COMPANIES[0])]
        cik = self._cik(query)
        if cik:
            company = self.company_details(cik)
            return [{key: company[key] for key in ("ticker", "cik", "name")}]
        needle = query.casefold()
        results = [company for company in self._companies() if needle in company["name"].casefold() or needle in company["ticker"].casefold()]
        results.sort(key=lambda company: (
            company["ticker"].casefold() != needle,
            company["name"].casefold() != needle,
            not company["name"].casefold().startswith(needle),
            company["name"].casefold(),
            company["ticker"],
        ))
        return [dict(company) for company in results[:20]]

    def company(self, identifier: str) -> dict:
        identifier = self._identifier(identifier)
        known = next((company for company in COMPANIES if company["ticker"] == identifier.upper()), None)
        if known:
            return dict(known)
        cik = self._cik(identifier)
        if cik:
            return {"ticker": f"CIK{cik}", "cik": cik, "name": f"SEC filer CIK {cik}"}
        needle = identifier.casefold()
        matches = [company for company in self._companies() if needle in (company["ticker"].casefold(), company["name"].casefold())]
        by_cik = {company["cik"]: company for company in matches}
        if len(by_cik) == 1:
            return dict(next(iter(by_cik.values())))
        if len(by_cik) > 1:
            raise ValueError("Several SEC filers match that name. Search and choose the specific company or enter its CIK.")
        raise ValueError("No exact SEC company match. Search by company name or ticker, or enter its SEC CIK.")

    def company_details(self, identifier: str) -> dict:
        company = self.company(identifier)
        if company["ticker"] == "DEMO":
            filing = self._fixture(company)
            return {
                **company, "offline": True, "filing_available": True,
                "industry": "Offline demonstration", "exchanges": [],
                "latest_10k": {"accession": filing.accession, "report_date": filing.report_date, "filing_date": None, "url": filing.url, "form": "10-K"},
            }
        with self._client() as client:
            submissions = self._submissions(client, company["cik"])
            return self._details(client, company, submissions)

    def load(self, ticker: str) -> Filing:
        filing = self.retrieve_filing(ticker)
        filing.company_facts = self.lookup_company_facts(ticker)
        return filing

    def retrieve_filing(self, ticker: str) -> Filing:
        company = self.company(ticker)
        if company["ticker"] == "DEMO":
            return self._fixture(company)
        return self._live_filing(company)

    def lookup_company_facts(self, ticker: str) -> dict:
        company = self.company(ticker)
        if company["ticker"] == "DEMO":
            fixture = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "demo_companyfacts.json"
            return json.loads(fixture.read_text(encoding="utf-8"))
        url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{company['cik']}.json"
        with self._client() as client:
            return self._json(client, url)

    def _submissions(self, client, cik: str) -> dict:
        try:
            submissions = self._json(client, f"https://data.sec.gov/submissions/CIK{cik}.json")
        except ValueError as error:
            raise ValueError(f"No SEC filer found for CIK {cik}.") from error
        if str(submissions["cik"]).zfill(10) != cik:
            raise ValueError("SEC returned a different company identifier; the filing was not used.")
        return submissions

    @staticmethod
    def _filing_url(cik: str, accession: str, document: str) -> str:
        if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession) or not re.fullmatch(r"[A-Za-z0-9_.-]+", document) or ".." in document:
            raise ValueError("SEC returned an invalid filing document path.")
        return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{document}"

    def _latest_10k(self, client, company: dict, submissions: dict) -> dict | None:
        batches = [submissions.get("filings", {}).get("recent", {})]
        archives = iter(sorted(submissions.get("filings", {}).get("files", []), key=lambda item: item.get("filingTo", ""), reverse=True))
        while True:
            batch = batches.pop()
            candidates = [i for i, form in enumerate(batch.get("form", [])) if form == "10-K" and batch["primaryDocument"][i]]
            if candidates:
                index = max(candidates, key=lambda i: batch["filingDate"][i])
                accession = batch["accessionNumber"][index]
                return {
                    "form": "10-K", "accession": accession,
                    "report_date": batch["reportDate"][index],
                    "filing_date": batch["filingDate"][index],
                    "url": self._filing_url(company["cik"], accession, batch["primaryDocument"][index]),
                }
            archive = next(archives, None)
            if archive is None:
                return None
            filename = archive["name"]
            if not re.fullmatch(rf"CIK{company['cik']}-submissions-\d+\.json", filename):
                raise ValueError("SEC returned an invalid historical submissions path.")
            batches.append(self._json(client, f"https://data.sec.gov/submissions/{filename}"))

    def _details(self, client, company: dict, submissions: dict) -> dict:
        tickers = submissions.get("tickers", [])
        ticker = company["ticker"] if company["ticker"] in tickers else (tickers[0] if tickers else f"CIK{company['cik']}")
        address = submissions.get("addresses", {}).get("business", {}) or {}
        latest = self._latest_10k(client, company, submissions)
        return {
            "ticker": ticker, "cik": company["cik"], "name": submissions["name"],
            "exchanges": submissions.get("exchanges", []),
            "industry": submissions.get("sicDescription", ""), "sic": submissions.get("sic", ""),
            "description": submissions.get("description", ""), "entity_type": submissions.get("entityType", ""),
            "fiscal_year_end": submissions.get("fiscalYearEnd", ""),
            "address": ", ".join(str(address[key]) for key in ("street1", "street2", "city", "stateOrCountryDescription", "zipCode") if address.get(key)),
            "source_url": f"https://data.sec.gov/submissions/CIK{company['cik']}.json",
            "offline": False, "filing_available": latest is not None, "latest_10k": latest,
        }

    def _live_filing(self, company: dict) -> Filing:
        with self._client() as client:
            submissions = self._submissions(client, company["cik"])
            company = self._details(client, company, submissions)
            latest = company["latest_10k"]
            if latest is None:
                raise ValueError(f"No Form 10-K is available for {company['name']} (CIK {company['cik']}). A 20-F or 40-F is not a 10-K.")
            response = self._get(client, latest["url"])
        return Filing(
            company=company, accession=latest["accession"], report_date=latest["report_date"],
            url=latest["url"], html=response.text, company_facts={},
        )

    @staticmethod
    def _get(client: httpx.Client, url: str):
        try:
            response = client.get(url)
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 404:
                raise ValueError("The requested SEC record was not found.") from error
            raise SecUnavailableError(f"SEC EDGAR is unavailable (HTTP {error.response.status_code}). Try again later and check the SEC_USER_AGENT contact configuration.") from error
        except httpx.RequestError as error:
            raise SecUnavailableError("Unable to connect to SEC EDGAR. Check your connection and try again.") from error

    @classmethod
    def _json(cls, client: httpx.Client, url: str) -> dict:
        try:
            return cls._get(client, url).json()
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise SecUnavailableError("SEC EDGAR returned an unreadable response. Try again later.") from error

    @staticmethod
    def _fixture(company: dict) -> Filing:
        fixture = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "demo_10k.html"
        return Filing(
            company=company,
            accession="0000000000-24-000001",
            report_date="2024-12-31",
            url="demo://10-k/0000000000-24-000001",
            html=fixture.read_text(encoding="utf-8"),
            company_facts={},
            offline=True,
        )
