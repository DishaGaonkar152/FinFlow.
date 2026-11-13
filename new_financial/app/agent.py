import copy
import operator
import sqlite3
import uuid
from pathlib import Path
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import InjectedState, ToolNode
from langgraph.types import Command, interrupt

from .extractor import FilingExtractor


class AgentState(TypedDict, total=False):
    run_id: str
    ticker: str
    confidence_threshold: float
    variance_threshold: float
    filing: dict
    company_facts: dict
    metrics: list[dict]
    segment_data: dict | None
    validated: bool
    escalation_count: int
    explanations_generated: bool
    report: dict | None
    status: str
    next_action: str
    messages: Annotated[list[BaseMessage], add_messages]
    tool_trace: Annotated[list[dict], operator.add]


class FinancialAuditorAgent:
    def __init__(self, operations, checkpoint_path: str):
        self.operations = operations
        Path(checkpoint_path).parent.mkdir(parents=True, exist_ok=True)
        self._checkpoint_connection = sqlite3.connect(checkpoint_path, check_same_thread=False)
        self._checkpoint_connection.execute("PRAGMA journal_mode = WAL")
        self.checkpointer = SqliteSaver(self._checkpoint_connection)
        self.checkpointer.setup()
        self.tools = self._create_tools()
        self.graph = self._compile_graph()

    def _create_tools(self):
        @tool("retrieve_filing", description="Call the retrieve_10k_filing MCP tool to obtain a Form 10-K and its source HTML from SEC EDGAR or the offline demo.")
        def retrieve_filing(
            ticker: str,
            tool_call_id: Annotated[str, InjectedToolCallId],
        ) -> Command:
            artifact = self.operations.mcp.call("retrieve_10k_filing", {"ticker": ticker})
            message = ToolMessage(
                content=f"MCP retrieved {ticker} Form 10-K {artifact['accession']} for period {artifact['report_date']}.",
                tool_call_id=tool_call_id,
                name="retrieve_filing",
            )
            return Command(update={"filing": artifact, "status": "processing", "messages": [message]})

        @tool("lookup_xbrl_company_facts", description="Call the lookup_xbrl_company_facts MCP tool to retrieve independent SEC XBRL ground truth for the filing company.")
        def lookup_xbrl_company_facts(
            state: Annotated[dict, InjectedState],
            tool_call_id: Annotated[str, InjectedToolCallId],
        ) -> Command:
            result = self.operations.mcp.call("lookup_xbrl_company_facts", {"ticker": state["ticker"]})
            message = ToolMessage(
                content=f"MCP retrieved XBRL Company Facts for {state['ticker']}.",
                tool_call_id=tool_call_id,
                name="lookup_xbrl_company_facts",
            )
            return Command(update={"company_facts": result["company_facts"], "messages": [message]})

        @tool("extract_metrics", description="Extract eight financial metrics with reporting contexts, units, confidence scores, source evidence, and independent XBRL ground truth.")
        def extract_metrics(
            state: Annotated[dict, InjectedState],
            tool_call_id: Annotated[str, InjectedToolCallId],
        ) -> Command:
            filing = state["filing"]
            extractor = FilingExtractor(filing["html"], filing["report_date"], filing["url"])
            metrics = extractor.extract()
            self.operations._ground_truth(state["company_facts"], metrics, filing["accession"])
            message = ToolMessage(
                content=f"Extracted {len(metrics)} evidence-backed metrics and attached available XBRL ground truth.",
                tool_call_id=tool_call_id,
                name="extract_metrics",
            )
            return Command(update={
                "metrics": metrics,
                "segment_data": extractor.segment_total(metrics[0]),
                "messages": [message],
            })

        @tool("validate_metrics", description="Apply accounting-equation, segment, year-on-year, and confidence checks; automatically approve eligible values and escalate the rest.")
        def validate_metrics(
            state: Annotated[dict, InjectedState],
            tool_call_id: Annotated[str, InjectedToolCallId],
        ) -> Command:
            metrics = copy.deepcopy(state["metrics"])
            self.operations._validate(metrics, state.get("segment_data"), state["variance_threshold"])
            for metric in metrics:
                failed = any(check["status"] == "failed" for check in metric["validations"])
                metric["original_value"] = metric["value"]
                metric["status"] = (
                    "pending"
                    if metric["value"] is None or failed or metric["confidence"] < state["confidence_threshold"]
                    else "auto_approved"
                )
            escalations = sum(metric["status"] == "pending" for metric in metrics)
            message = ToolMessage(
                content=f"Validation complete: {len(metrics) - escalations} auto-approved and {escalations} escalated.",
                tool_call_id=tool_call_id,
                name="validate_metrics",
            )
            return Command(update={
                "metrics": metrics,
                "validated": True,
                "escalation_count": escalations,
                "status": "awaiting_review" if escalations else "processing",
                "messages": [message],
            })

        @tool("explain_metrics", description="Explain every extracted metric's meaning, extraction evidence, confidence, validation status, and review guidance using the configured LLM.")
        def explain_metrics(
            state: Annotated[dict, InjectedState],
            tool_call_id: Annotated[str, InjectedToolCallId],
        ) -> Command:
            metrics = copy.deepcopy(state["metrics"])
            explanations = self.operations.explainer.explain_metrics(metrics)
            for metric in metrics:
                if metric["key"] in explanations:
                    metric["explanation"] = explanations[metric["key"]]
            ai_count = sum(item["generated_by"] == "llm" for item in explanations.values())
            message = ToolMessage(
                content=f"Generated explanations for {len(explanations)} metrics: {ai_count} LLM-generated and {len(explanations) - ai_count} rule-based fallbacks.",
                tool_call_id=tool_call_id,
                name="explain_metrics",
            )
            return Command(update={
                "metrics": metrics,
                "explanations_generated": True,
                "messages": [message],
            })

        @tool("verify_ground_truth", description="Compare final values against SEC XBRL Company Facts and produce extraction accuracy and review-outcome reports.")
        def verify_ground_truth(
            state: Annotated[dict, InjectedState],
            tool_call_id: Annotated[str, InjectedToolCallId],
        ) -> Command:
            metrics = copy.deepcopy(state["metrics"])
            compared = 0
            matches = 0
            for metric in metrics:
                truth = metric.get("ground_truth")
                if not truth:
                    metric["verification"] = {"status": "unavailable", "matches": None, "relative_error": None}
                    continue
                compared += 1
                actual = metric["value"]
                expected = truth["value"]
                if actual is None:
                    match = False
                    relative_error = None
                else:
                    difference = abs(actual - expected)
                    tolerance = 0.000001 if metric["unit"] == "USD/shares" else max(1, abs(expected) * 0.000001)
                    match = difference <= tolerance
                    relative_error = difference / abs(expected) if expected else difference
                matches += int(match)
                metric["verification"] = {
                    "status": "matched" if match else "mismatched",
                    "matches": match,
                    "relative_error": relative_error,
                }
            reviewed_statuses = {"approved", "corrected", "rejected"}
            report = {
                "metrics_extracted": len(metrics),
                "ground_truth_compared": compared,
                "ground_truth_matches": matches,
                "accuracy_percentage": round(matches / compared * 100, 2) if compared else None,
                "automatically_approved": sum(metric["status"] == "auto_approved" for metric in metrics),
                "escalated": state["escalation_count"],
                "completed_reviews": sum(metric["status"] in reviewed_statuses for metric in metrics),
                "pending_reviews": sum(metric["status"] == "pending" for metric in metrics),
                "corrected": sum(metric["status"] == "corrected" for metric in metrics),
                "rejected": sum(metric["status"] == "rejected" for metric in metrics),
            }
            message = ToolMessage(
                content=f"Final XBRL verification complete: {matches}/{compared} compared values matched.",
                tool_call_id=tool_call_id,
                name="verify_ground_truth",
            )
            return Command(update={
                "metrics": metrics,
                "report": report,
                "status": "completed",
                "messages": [message],
            })

        return [retrieve_filing, lookup_xbrl_company_facts, extract_metrics, validate_metrics, explain_metrics, verify_ground_truth]

    def _compile_graph(self):
        builder = StateGraph(AgentState)
        builder.add_node("select_tool", self._select_tool)
        builder.add_node("tools", ToolNode(self.tools, handle_tool_errors=False))
        builder.add_node("human_review", self._human_review)
        builder.add_edge(START, "select_tool")
        builder.add_conditional_edges("select_tool", self._route, {
            "tools": "tools",
            "human_review": "human_review",
            "end": END,
        })
        builder.add_edge("tools", "select_tool")
        builder.add_edge("human_review", "select_tool")
        return builder.compile(checkpointer=self.checkpointer)

    @staticmethod
    def _selection(state: AgentState) -> tuple[str, str, dict]:
        if not state.get("filing"):
            return "retrieve_filing", "No filing artifact is present; the SEC filing MCP tool is required first.", {"ticker": state["ticker"]}
        if "company_facts" not in state:
            return "lookup_xbrl_company_facts", "The filing is available; the independent XBRL lookup MCP tool must fetch ground truth.", {}
        if not state.get("metrics"):
            return "extract_metrics", "Filing and ground truth are available; evidence-backed metric extraction is next.", {}
        if not state.get("validated"):
            return "validate_metrics", "Metrics are extracted; accounting, variance, and confidence tools must determine eligibility.", {}
        if not state.get("explanations_generated"):
            return "explain_metrics", f"All {len(state['metrics'])} extracted metrics need grounded reasons for their source values and validation outcomes.", {}
        pending = sum(metric["status"] == "pending" for metric in state["metrics"])
        if pending:
            return "human_review", f"{pending} values have low confidence or failed validations; autonomous processing must pause.", {}
        if not state.get("report"): 
            return "verify_ground_truth", "All values are approved or corrected; independent XBRL verification is now safe to run.", {}
        return "end", "The final accuracy report is present; no further tool call is needed.", {}

    def _select_tool(self, state: AgentState) -> dict:
        selected, reason, arguments = self._selection(state)
        trace = {
            "sequence": len(state.get("tool_trace", [])) + 1,
            "selected_tool": selected,
            "reason": reason,
        }
        if selected in {"human_review", "end"}:
            message = AIMessage(content=reason)
        else:
            message = AIMessage(
                content=f"Selected tool: {selected}. {reason}",
                tool_calls=[{"name": selected, "args": arguments, "id": str(uuid.uuid4()), "type": "tool_call"}],
            )
        return {"next_action": selected, "messages": [message], "tool_trace": [trace]}

    @staticmethod
    def _route(state: AgentState) -> str:
        if state["next_action"] in {"human_review", "end"}:
            return state["next_action"]
        return "tools"

    @staticmethod
    def _human_review(state: AgentState) -> dict:
        pending = [metric["key"] for metric in state["metrics"] if metric["status"] == "pending"]
        decision = interrupt({"run_id": state["run_id"], "pending_metrics": pending})
        metrics = copy.deepcopy(state["metrics"])
        metric = next(item for item in metrics if item["key"] == decision["metric_key"])
        metric["status"] = {"approve": "approved", "correct": "corrected", "reject": "rejected"}[decision["decision"]]
        if decision["decision"] == "correct":
            metric["value"] = float(decision["corrected_value"])
        elif decision["decision"] == "reject":
            metric["value"] = None
        trace = {
            "sequence": len(state.get("tool_trace", [])) + 1,
            "selected_tool": "human_decision",
            "reason": f"{decision['reviewer']} selected {decision['decision']} for {decision['metric_key']}; graph resumed from its checkpoint.",
        }
        return {"metrics": metrics, "tool_trace": [trace]}

    @staticmethod
    def _config(run_id: str) -> dict:
        return {"configurable": {"thread_id": run_id}}

    def start(self, run_id: str, ticker: str, confidence_threshold: float, variance_threshold: float) -> dict:
        initial = {
            "run_id": run_id,
            "ticker": ticker,
            "confidence_threshold": confidence_threshold,
            "variance_threshold": variance_threshold,
            "metrics": [],
            "validated": False,
            "explanations_generated": False,
            "report": None,
            "status": "processing",
            "tool_trace": [],
            "messages": [],
        }
        return self.graph.invoke(initial, self._config(run_id))

    def resume(self, run_id: str, decision: dict) -> dict:
        return self.graph.invoke(Command(resume=decision), self._config(run_id))

    def close(self) -> None:
        self._checkpoint_connection.close()
