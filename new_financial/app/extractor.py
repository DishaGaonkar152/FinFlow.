import re
import warnings
from dataclasses import dataclass
from datetime import date

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning


@dataclass(frozen=True)
class MetricSpec:
    key: str
    label: str
    tags: tuple[str, ...]
    unit: str
    duration: bool


METRIC_SPECS = [
    MetricSpec("revenue", "Revenue", ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet"), "USD", True),
    MetricSpec("net_income", "Net Income", ("NetIncomeLoss", "ProfitLoss"), "USD", True),
    MetricSpec("total_assets", "Total Assets", ("Assets",), "USD", False),
    MetricSpec("total_liabilities", "Total Liabilities", ("Liabilities",), "USD", False),
    MetricSpec("shareholders_equity", "Shareholders’ Equity", ("StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest", "StockholdersEquity"), "USD", False),
    MetricSpec("operating_cash_flow", "Operating Cash Flow", ("NetCashProvidedByUsedInOperatingActivities", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"), "USD", True),
    MetricSpec("cash_and_equivalents", "Cash & Cash Equivalents", ("CashAndCashEquivalentsAtCarryingValue",), "USD", False),
    MetricSpec("diluted_eps", "Diluted EPS", ("EarningsPerShareDiluted",), "USD/shares", True),
]


def local_name(value: str) -> str:
    return value.split(":")[-1].split("}")[-1].lower()


def element_name(element) -> str:
    return local_name(element.name or "")


def child_text(element, name: str) -> str | None:
    child = element.find(lambda tag: element_name(tag) == name)
    return child.get_text(strip=True) if child else None


def previous_date(value: str | None) -> str | None:
    if not value:
        return None
    parsed = date.fromisoformat(value)
    try:
        return parsed.replace(year=parsed.year - 1).isoformat()
    except ValueError:
        return parsed.replace(year=parsed.year - 1, day=28).isoformat()


def parse_number(fact) -> float | None:
    text = fact.get_text("", strip=True).replace(",", "").replace("$", "").replace("−", "-").strip()
    if not text or text in {"—", "-", "–"}:
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    match = re.search(r"[-+]?\d*\.?\d+", text)
    if not match:
        return None
    value = float(match.group())
    if negative:
        value = -value
    value *= 10 ** int(fact.get("scale", 0))
    if fact.get("sign") == "-":
        value = -value
    return value


class FilingExtractor:
    def __init__(self, html: str, report_date: str, filing_url: str):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
            self.soup = BeautifulSoup(html, "lxml")
        self.report_date = report_date
        self.filing_url = filing_url
        self.contexts = self._contexts()
        self.units = self._units()
        self.facts = self._facts()

    def _contexts(self) -> dict:
        contexts = {}
        for context in self.soup.find_all(lambda tag: element_name(tag) == "context"):
            context_id = context.get("id")
            if not context_id:
                continue
            dimensions = []
            for member in context.find_all(lambda tag: element_name(tag) == "explicitmember"):
                dimensions.append({"axis": member.get("dimension", ""), "member": member.get_text(strip=True)})
            contexts[context_id] = {
                "start_date": child_text(context, "startdate"),
                "end_date": child_text(context, "enddate"),
                "instant": child_text(context, "instant"),
                "dimensions": dimensions,
            }
        return contexts

    def _units(self) -> dict:
        units = {}
        for unit in self.soup.find_all(lambda tag: element_name(tag) == "unit"):
            unit_id = unit.get("id")
            measures = [tag.get_text(strip=True) for tag in unit.find_all(lambda tag: element_name(tag) == "measure")]
            if unit_id and measures:
                units[unit_id] = "/".join(m.split(":")[-1] for m in measures)
        return units

    def _facts(self) -> list[dict]:
        facts = []
        for element in self.soup.find_all(lambda tag: element_name(tag) == "nonfraction"):
            name = element.get("name", "")
            context_ref = element.get("contextref", "")
            context = self.contexts.get(context_ref)
            if not name or context is None:
                continue
            source_element = element.parent
            if source_element and element_name(source_element) == "td" and source_element.parent:
                source_element = source_element.parent
            parent_text = source_element.get_text(" ", strip=True) if source_element else element.get_text(" ", strip=True)
            facts.append({
                "name": name.split(":")[-1],
                "full_name": name,
                "value": parse_number(element),
                "unit": self.units.get(element.get("unitref", ""), element.get("unitref", "")),
                "context_ref": context_ref,
                **context,
                "source_snippet": parent_text[:700],
            })
        return facts

    def _candidates(self, spec: MetricSpec, period_offset: int = 0) -> list[dict]:
        target_end = self.report_date if period_offset == 0 else previous_date(self.report_date)
        candidates = []
        for fact in self.facts:
            if fact["name"] not in spec.tags or fact["unit"] != spec.unit or fact["dimensions"]:
                continue
            if spec.duration:
                if fact["end_date"] != target_end or not fact["start_date"]:
                    continue
                days = (date.fromisoformat(fact["end_date"]) - date.fromisoformat(fact["start_date"])).days
                if not 300 <= days <= 450:
                    continue
            elif fact["instant"] != target_end:
                continue
            if fact["value"] is not None:
                candidates.append(fact)
        candidates.sort(key=lambda fact: spec.tags.index(fact["name"]))
        return candidates

    @staticmethod
    def _conflicting_values(candidates: list[dict]) -> bool:
        values = [fact["value"] for fact in candidates]
        if len(values) < 2:
            return False
        first = values[0]
        return any(abs(value - first) > max(1, abs(first) * 0.000001) for value in values[1:])

    def extract(self) -> list[dict]:
        metrics = []
        for spec in METRIC_SPECS:
            candidates = self._candidates(spec)
            previous_candidates = self._candidates(spec, period_offset=1)
            selected = candidates[0] if candidates else None
            conflicting = self._conflicting_values(candidates)
            confidence_reasons = []
            if selected:
                tag_rank = spec.tags.index(selected["name"])
                confidence = 0.98 if tag_rank == 0 else 0.90
                if tag_rank > 0:
                    confidence_reasons.append("Alternative XBRL tag used")
                if conflicting:
                    confidence = min(confidence, 0.60)
                    confidence_reasons.append("Conflicting values found for the same tag and reporting context")
                value = selected["value"]
                period = selected["end_date"] or selected["instant"]
                metric = {
                    "key": spec.key,
                    "label": spec.label,
                    "value": value,
                    "previous_value": previous_candidates[0]["value"] if previous_candidates else None,
                    "unit": spec.unit,
                    "period": period,
                    "start_date": selected["start_date"],
                    "end_date": selected["end_date"] or selected["instant"],
                    "source_name": selected["full_name"],
                    "source_snippet": selected["source_snippet"],
                    "source_url": self.filing_url,
                    "context_ref": selected["context_ref"],
                    "confidence": confidence,
                    "confidence_reasons": confidence_reasons,
                }
            else:
                metric = {
                    "key": spec.key,
                    "label": spec.label,
                    "value": None,
                    "previous_value": None,
                    "unit": spec.unit,
                    "period": self.report_date,
                    "start_date": None,
                    "end_date": self.report_date,
                    "source_name": ", ".join(spec.tags),
                    "source_snippet": "No matching consolidated annual inline-XBRL fact was found in the filing.",
                    "source_url": self.filing_url,
                    "context_ref": None,
                    "confidence": 0.0,
                    "confidence_reasons": ["Required fact is missing or has an unsupported period/unit"],
                }
            metrics.append(metric)

        by_key = {metric["key"]: metric for metric in metrics}
        liabilities = by_key["total_liabilities"]
        assets = by_key["total_assets"]
        equity = by_key["shareholders_equity"]
        if liabilities["value"] is None and assets["value"] is not None and equity["value"] is not None:
            liabilities.update({
                "value": assets["value"] - equity["value"],
                "previous_value": (
                    assets["previous_value"] - equity["previous_value"]
                    if assets["previous_value"] is not None and equity["previous_value"] is not None else None
                ),
                "source_name": f"Derived: {assets['source_name']} − {equity['source_name']}",
                "source_snippet": (
                    f"Total liabilities derived as assets minus total equity. Assets: {assets['source_snippet']} "
                    f"Total equity: {equity['source_snippet']}"
                )[:700],
                "context_ref": f"{assets['context_ref']} + {equity['context_ref']}",
                "confidence": 0.70,
                "confidence_reasons": ["Derived from assets minus total equity because the filing has no separate total-liabilities tag"],
            })
        return metrics

    def segment_total(self, revenue_metric: dict) -> dict | None:
        spec = METRIC_SPECS[0]
        segments = {}
        for fact in self.facts:
            if fact["name"] not in spec.tags or fact["unit"] != "USD" or fact["value"] is None:
                continue
            if fact["start_date"] != revenue_metric["start_date"] or fact["end_date"] != revenue_metric["end_date"]:
                continue
            if len(fact["dimensions"]) != 1 or "segment" not in fact["dimensions"][0]["axis"].lower():
                continue
            segments[fact["dimensions"][0]["member"]] = fact["value"]
        if len(segments) < 2:
            return None
        return {"members": sorted(segments), "total": sum(segments.values())}
