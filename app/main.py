import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from .auth import AuthMiddleware, AuthStore, COOKIE_NAME, LoginRequest, OwnerSetupRequest, SESSION_SECONDS
from .db import Database
from .models import ChatRequest, ReviewRequest, RunRequest
from .reports import generate_report
from .sec import COMPANIES, SecUnavailableError
from .workflow import AuditWorkflow


BASE_DIR = Path(__file__).resolve().parent
database = Database(os.getenv("AUDITOR_DB_PATH", str(BASE_DIR.parent / "data" / "auditor.db")))
auth_store = AuthStore(os.getenv("FINFLOW_AUTH_DB_PATH", database.path + ".auth"))
workflow = AuditWorkflow(database)


@asynccontextmanager
async def lifespan(_):
    try:
        yield
    finally:
        workflow.close()


app = FastAPI(title="Human-in-the-Loop Financial Filing Auditor", version="1.0.0", lifespan=lifespan)
app.add_middleware(AuthMiddleware, store=auth_store)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.get("/login", include_in_schema=False)
def login_page(request: Request):
    if request.state.auth_session:
        return RedirectResponse("/", status_code=303)
    return FileResponse(BASE_DIR / "static" / "login.html")


@app.get("/api/auth/session", include_in_schema=False)
def auth_session(request: Request):
    session = request.state.auth_session
    if not session:
        return {"authenticated": False, "configured": auth_store.configured()}
    return {"authenticated": True, "username": session["username"], "csrf_token": session["csrf_token"]}


@app.post("/api/auth/login", include_in_schema=False)
def login(credentials: LoginRequest, request: Request):
    auth_store.authenticate(credentials.username, credentials.password)
    auth_store.revoke_session(request.cookies.get(COOKIE_NAME))
    token, _ = auth_store.create_session()
    response = JSONResponse({"authenticated": True})
    response.set_cookie(COOKIE_NAME, token, max_age=SESSION_SECONDS, httponly=True, secure=request.url.scheme == "https", samesite="strict", path="/")
    return response


@app.post("/api/auth/setup", include_in_schema=False)
def setup_owner(credentials: OwnerSetupRequest, request: Request):
    username = credentials.username.strip()
    if not username:
        raise HTTPException(status_code=400, detail="Enter a username.")
    try:
        auth_store.initialize_owner(username, credentials.password)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return login(LoginRequest(username=username, password=credentials.password), request)


@app.post("/api/auth/logout", include_in_schema=False)
def logout(request: Request):
    auth_store.revoke_session(request.cookies.get(COOKIE_NAME))
    response = JSONResponse({"authenticated": False})
    response.delete_cookie(COOKIE_NAME, httponly=True, secure=request.url.scheme == "https", samesite="strict", path="/")
    return response


def run_detail(run_id: str) -> dict:
    run = workflow.db.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    run["reviews"] = workflow.db.get_reviews(run_id)
    run["audit_log"] = workflow.db.get_audit(run_id)
    run["audit_verification"] = workflow.db.verify_audit(run_id)
    return run


@app.get("/")
def dashboard():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "agent_framework": "LangGraph",
        "dynamic_tool_selection": True,
        "mcp_transport": "stdio",
        "mcp_tools": ["retrieve_10k_filing", "lookup_xbrl_company_facts"],
        "ai_explanations": {
            "configured": workflow.explainer.configured,
            "provider": "openai_compatible" if workflow.explainer.configured else "deterministic_rules",
            "model": workflow.explainer.model if workflow.explainer.configured else None,
        },
        "dashboard_assistant": {
            "configured": workflow.assistant.configured,
            "provider": "openai_compatible" if workflow.assistant.configured else "deterministic_rules",
            "model": workflow.assistant.model if workflow.assistant.configured else None,
        },
    }


@app.get("/api/mcp/tools")
def mcp_tools():
    return workflow.mcp.list_tools()


@app.get("/api/companies")
def companies():
    return COMPANIES


@app.get("/api/companies/search")
def search_companies(q: str = Query(min_length=1, max_length=160)):
    try:
        return workflow.sec.search_companies(q)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except SecUnavailableError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@app.get("/api/companies/{identifier}")
def company_details(identifier: str):
    try:
        return workflow.sec.company_details(identifier)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except SecUnavailableError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@app.post("/api/runs")
def create_run(request: RunRequest):
    try:
        run = workflow.start(request.ticker, request.confidence_threshold, request.variance_threshold)
        return run_detail(run["id"])
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except SecUnavailableError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@app.get("/api/runs")
def runs():
    return workflow.db.list_runs()


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    return run_detail(run_id)


@app.get("/api/runs/{run_id}/report")
def financial_report(run_id: str, format: Literal["pdf", "docx"] = "pdf"):
    run = run_detail(run_id)
    content = generate_report(run, format)
    ticker = re.sub(r"[^A-Za-z0-9_-]", "", run["state"]["company"]["ticker"])
    identifier = re.sub(r"[^A-Za-z0-9_-]", "", run_id)[:8]
    filename = f"FinFlow_{ticker}_{identifier}.{format}"
    media_type = "application/pdf" if format == "pdf" else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    return Response(content, media_type=media_type, headers={
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    })


@app.post("/api/runs/{run_id}/reviews/{review_id}")
def decide(run_id: str, review_id: str, request: ReviewRequest):
    try:
        workflow.submit_decision(run_id, review_id, request)
        return run_detail(run_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/assistant")
def dashboard_assistant(request: ChatRequest):
    question = request.message.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Enter a question")
    run = run_detail(request.run_id) if request.run_id else None
    history = [message.model_dump() for message in request.history]
    return workflow.assistant.answer(question, run, history)


@app.get("/api/runs/{run_id}/audit/verify")
def verify_audit(run_id: str):
    if not workflow.db.get_run(run_id):
        raise HTTPException(status_code=404, detail="Run not found")
    return workflow.db.verify_audit(run_id)
