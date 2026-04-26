"""FastAPI application entry point.

Endpoints
─────────
GET  /              → Ingress chat UI (HTML)
GET  /health        → JSON status
POST /v1/chat/completions  → OpenAI-compatible agent endpoint
GET  /api/audit     → Recent audit log entries (?limit=N)
GET  /debug/selftest → Connectivity / config checks
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from agent import Agent
from auth.routes import router as auth_router
from logging_utils import configure_logging, sanitize_for_log
from schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatCompletionChoice,
    ChatMessage,
    Role,
    Usage,
)
from storage import ensure_dirs, read_audit

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="HA AI Operator",
    version="0.1.0",
    docs_url="/api/docs",
    redoc_url=None,
    openapi_url="/api/openapi.json",
)

ensure_dirs()
configure_logging()
log = logging.getLogger("ha_ai_operator.main")

app.include_router(auth_router)


class FrontendLogEvent(BaseModel):
    level: str = "info"
    event: str = "ui.event"
    message: str = ""
    context: dict[str, Any] = Field(default_factory=dict)
    ts: str = ""
    url: str = ""
    session_id: str = ""


def _request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", "-"))


def _frontend_log_fn(level: str) -> Any:
    normalized = (level or "info").strip().lower()
    if normalized == "debug":
        return log.debug
    if normalized in ("warn", "warning"):
        return log.warning
    if normalized == "error":
        return log.error
    return log.info


@app.middleware("http")
async def request_logging_middleware(request: Request, call_next: Any) -> Any:
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    request.state.request_id = request_id
    started = time.perf_counter()
    path = request.url.path
    if request.url.query:
        path = f"{path}?{request.url.query}"
    client_host = request.client.host if request.client else "-"
    user_agent = (request.headers.get("user-agent", "-") or "-")[:160]
    log.info(
        "[req:%s] %s %s start client=%s ua=%s",
        request_id,
        request.method,
        path,
        client_host,
        user_agent,
    )
    try:
        response = await call_next(request)
    except Exception:
        duration_ms = (time.perf_counter() - started) * 1000
        log.exception(
            "[req:%s] %s %s failed duration_ms=%.1f",
            request_id,
            request.method,
            path,
            duration_ms,
        )
        raise

    duration_ms = (time.perf_counter() - started) * 1000
    response.headers["X-Request-ID"] = request_id
    log.info(
        "[req:%s] %s %s done status=%s duration_ms=%.1f",
        request_id,
        request.method,
        path,
        response.status_code,
        duration_ms,
    )
    return response


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health(request: Request) -> dict[str, Any]:
    payload = {
        "status": "ok",
        "version": "0.1.0",
        "mode": os.environ.get("MODE", "unknown"),
        "llm_provider": os.environ.get("LLM_PROVIDER", "unknown"),
        "llm_model": os.environ.get("LLM_MODEL", ""),
        "llm_model_set": bool(os.environ.get("LLM_MODEL", "")),
        "llm_base_url_set": bool(os.environ.get("LLM_BASE_URL", "")),
        "llm_oauth_token_set": bool(os.environ.get("LLM_OAUTH_TOKEN", "")),
        "allow_supervisor_api": os.environ.get("ALLOW_SUPERVISOR_API", "false"),
        "confirmation_required": os.environ.get("CONFIRMATION_REQUIRED", "true"),
        "max_actions_per_turn": os.environ.get("MAX_ACTIONS_PER_TURN", "5"),
        "audit_log_level": os.environ.get("AUDIT_LOG_LEVEL", "minimal"),
        "app_log_level": os.environ.get("APP_LOG_LEVEL", "info"),
    }
    log.info(
        "[req:%s] health payload mode=%s provider=%s model=%s app_log_level=%s sup_api=%s",
        _request_id(request),
        payload["mode"],
        payload["llm_provider"],
        payload["llm_model"] or "<unset>",
        payload["app_log_level"],
        payload["allow_supervisor_api"],
    )
    return payload


# ── Chat completions ──────────────────────────────────────────────────────────

@app.post("/v1/chat/completions", response_model=ChatCompletionResponse)
async def chat_completions(
    req: ChatCompletionRequest, request: Request
) -> ChatCompletionResponse:
    req_id = _request_id(request)
    log.info(
        "[req:%s] chat start model=%s messages=%s temperature=%s",
        req_id,
        req.model,
        len(req.messages),
        req.temperature,
    )
    agent = Agent()
    try:
        content = await agent.process(req)
    except Exception as exc:
        log.exception("[req:%s] chat failed", req_id)
        raise HTTPException(status_code=500, detail=f"Agent error: {exc}") from exc

    log.info("[req:%s] chat done reply_chars=%s", req_id, len(content or ""))
    return ChatCompletionResponse(
        model=req.model,
        choices=[
            ChatCompletionChoice(
                index=0,
                message=ChatMessage(role=Role.assistant, content=content),
                finish_reason="stop",
            )
        ],
        usage=Usage(),
    )


# ── Audit log ─────────────────────────────────────────────────────────────────

@app.get("/api/audit")
async def get_audit(request: Request, limit: int = 50) -> dict[str, Any]:
    limit = max(1, min(limit, 500))
    entries = read_audit(limit=limit)
    log.info(
        "[req:%s] audit fetched limit=%s returned=%s",
        _request_id(request),
        limit,
        len(entries),
    )
    return {"entries": entries, "count": len(entries)}


@app.post("/api/frontend-log")
async def frontend_log(event: FrontendLogEvent, request: Request) -> dict[str, bool]:
    req_id = _request_id(request)
    fn = _frontend_log_fn(event.level)
    safe_context = sanitize_for_log(event.context)
    safe_message = sanitize_for_log(event.message)
    fn(
        "[req:%s] [frontend session=%s] event=%s message=%s ts=%s url=%s context=%s",
        req_id,
        (event.session_id or "-")[:32],
        (event.event or "ui.event")[:96],
        str(safe_message)[:280],
        (event.ts or "-")[:48],
        (event.url or "-")[:220],
        json.dumps(safe_context, default=str)[:1200],
    )
    return {"ok": True}


# ── Self-test ─────────────────────────────────────────────────────────────────

@app.get("/debug/selftest")
async def selftest(request: Request) -> dict[str, Any]:
    checks: dict[str, Any] = {}

    token = os.environ.get("SUPERVISOR_TOKEN", "")
    checks["supervisor_token_present"] = bool(token)

    if token:
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                r = await client.get(
                    "http://supervisor/core/api/config",
                    headers={"Authorization": f"Bearer {token}"},
                )
            checks["ha_api_reachable"] = r.status_code == 200
            checks["ha_api_status_code"] = r.status_code
        except Exception as exc:
            checks["ha_api_reachable"] = False
            checks["ha_api_error"] = str(exc)
    else:
        checks["ha_api_reachable"] = False
        checks["ha_api_error"] = "SUPERVISOR_TOKEN not set"

    checks["mode"] = os.environ.get("MODE", "unset")
    checks["llm_provider"] = os.environ.get("LLM_PROVIDER", "unset")
    checks["llm_base_url_set"] = bool(os.environ.get("LLM_BASE_URL", ""))
    checks["llm_oauth_token_set"] = bool(os.environ.get("LLM_OAUTH_TOKEN", ""))
    checks["allow_supervisor_api"] = os.environ.get("ALLOW_SUPERVISOR_API", "false")

    all_ok: bool = bool(
        checks.get("supervisor_token_present") and checks.get("ha_api_reachable")
    )
    log.info(
        "[req:%s] selftest ok=%s token_present=%s ha_api_reachable=%s ha_status=%s",
        _request_id(request),
        all_ok,
        checks.get("supervisor_token_present"),
        checks.get("ha_api_reachable"),
        checks.get("ha_api_status_code"),
    )
    return {"ok": all_ok, "checks": checks}


# ── Ingress UI ────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def ui_root(request: Request) -> HTMLResponse:
    log.info("[req:%s] serving ui root", _request_id(request))
    return HTMLResponse(content=_UI_HTML)


# Catch any unknown sub-path so the Ingress panel doesn't 404 on reload.
@app.get("/{full_path:path}", response_class=HTMLResponse, include_in_schema=False)
async def ui_catchall(full_path: str, request: Request) -> HTMLResponse:
    # Let actual API routes bubble up as 404; only serve UI for unknown paths.
    api_prefixes = ("v1/", "api/", "health", "debug/", "auth/", "oauth/")
    if any(full_path.startswith(p) for p in api_prefixes):
        log.info("[req:%s] catchall 404 for api-like path=%s", _request_id(request), full_path)
        raise HTTPException(status_code=404, detail="Not found")
    log.info("[req:%s] catchall serving ui for path=%s", _request_id(request), full_path)
    return HTMLResponse(content=_UI_HTML)


# ── UI HTML (self-contained, no external CDN) ─────────────────────────────────
# Dynamic values (mode, provider…) are loaded at runtime by JS via GET /health.
# All fetch() calls use relative paths so HA Ingress routing works correctly.

_UI_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>HA AI Operator</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    :root {
      --ha-blue: #03a9f4;
      --ha-blue-dark: #0288d1;
      --bg: #f4f6fb;
      --surface: #ffffff;
      --surface-soft: #eef3f8;
      --surface-raised: #ffffff;
      --border: #d9e2ec;
      --text: #1f2937;
      --muted: #697586;
      --subtle: #8a97a8;
      --shadow: 0 1px 2px rgba(15, 23, 42, .08), 0 8px 24px rgba(15, 23, 42, .06);
      --green: #0f9d58;
      --orange: #f2994a;
      --red: #db4437;
      --radius: 8px;
      --input-bg: #ffffff;
      --code-bg: #e8eef5;
    }
    @media (prefers-color-scheme: dark) {
      :root {
        --bg: #111827;
        --surface: #1f2937;
        --surface-soft: #172033;
        --surface-raised: #223044;
        --border: #344256;
        --text: #e5eef8;
        --muted: #a5b4c4;
        --subtle: #7d8da0;
        --shadow: 0 1px 2px rgba(0, 0, 0, .28), 0 12px 32px rgba(0, 0, 0, .2);
        --input-bg: #111827;
        --code-bg: #111827;
      }
    }
    body { font-family: -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
           background: var(--bg); color: var(--text); height: 100vh;
           display: flex; flex-direction: column; overflow: hidden;
           font-size: 14px; letter-spacing: 0; }
    /* ── header ── */
    header { background: var(--ha-blue); color: #fff; min-height: 56px;
             padding: 0 18px; display: flex; align-items: center; gap: 12px;
             flex-shrink: 0; box-shadow: 0 1px 0 rgba(0,0,0,.1); }
    header h1 { font-size: 1rem; font-weight: 600; color: #fff; letter-spacing: 0; }
    .badges { margin-left: auto; display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
    .badge { padding: 5px 10px; border-radius: 999px; font-size: .72rem;
             font-weight: 700; white-space: nowrap; background: rgba(255,255,255,.2);
             color: #fff; border: 1px solid rgba(255,255,255,.24); }
    .badge-read_only,
    .badge-control_assist,
    .badge-ops_write,
    .badge-sup,
    .badge-prov { background: rgba(255,255,255,.2); color: #fff; border-color: rgba(255,255,255,.28); }
    /* ── tab nav ── */
    .tabs { background: var(--surface); border-bottom: 1px solid var(--border);
            display: flex; gap: 4px; flex-shrink: 0; padding: 0 12px; }
    .tab-btn { background: none; border: none; border-bottom: 3px solid transparent;
               color: var(--muted); cursor: pointer; padding: 12px 18px 10px;
               font-size: .86rem; font-weight: 600; transition: color .15s, background .15s;
               min-width: 72px; }
    .tab-btn:hover { color: var(--text); background: var(--surface-soft); }
    .tab-btn.active { color: var(--ha-blue); border-bottom-color: var(--ha-blue); }
    /* ── tab content ── */
    .tab-content { display: none; flex: 1; overflow: hidden; }
    .tab-content.active { display: flex; }
    /* ── main layout (chat+audit) ── */
    .layout { display: flex; flex: 1; overflow: hidden; padding: 16px; gap: 16px; }
    /* ── chat panel ── */
    .chat { flex: 1; display: flex; flex-direction: column;
            gap: 12px; min-width: 0; }
    .messages { flex: 1; overflow-y: auto; display: flex;
                flex-direction: column; gap: 12px; padding: 4px 4px 10px; }
    .msg { padding: 12px 14px; border-radius: var(--radius); max-width: min(760px, 84%);
           line-height: 1.55; word-wrap: break-word; white-space: pre-wrap; box-shadow: var(--shadow); }
    .msg.user      { background: var(--ha-blue); color: #fff; align-self: flex-end;
                     border-bottom-right-radius: 3px; }
    .msg.assistant { background: var(--surface-raised); align-self: flex-start;
                     border: 1px solid var(--border); border-bottom-left-radius: 3px; }
    .msg.system    { background: transparent; align-self: center;
                     color: var(--muted); font-size: .82rem; font-style: normal;
                     box-shadow: none; max-width: 100%; }
    .msg code   { background: var(--code-bg); padding: 2px 5px; border-radius: 4px;
                  font-family: monospace; font-size: .88em; }
    .msg.user code { background: rgba(255,255,255,.18); }
    .msg strong { color: var(--ha-blue); }
    .msg.user strong { color: #fff; }
    .typing { color: var(--muted); font-style: normal; font-size: .82rem;
              padding: 2px 0; flex-shrink: 0; }
    .input-row { display: flex; gap: 8px; flex-shrink: 0; }
    textarea { flex: 1; background: var(--input-bg); border: 1px solid var(--border);
               border-radius: var(--radius); color: var(--text); padding: 12px 13px;
               font-size: .93rem; resize: none; font-family: inherit;
               min-height: 46px; max-height: 140px; box-shadow: var(--shadow); }
    textarea:focus { outline: none; border-color: var(--ha-blue); box-shadow: 0 0 0 3px rgba(3,169,244,.16); }
    .send { background: var(--ha-blue); color: #fff; border: none;
            border-radius: var(--radius); padding: 0 22px; font-weight: 700;
            cursor: pointer; align-self: stretch; min-width: 86px; }
    .send:hover { background: var(--ha-blue-dark); }
    .send:disabled { background: var(--border); color: var(--subtle);
                     cursor: not-allowed; }
    /* ── audit panel ── */
    .audit { width: 370px; background: var(--surface);
             border: 1px solid var(--border); border-radius: var(--radius);
             display: flex; flex-direction: column; overflow: hidden; flex-shrink: 0;
             box-shadow: var(--shadow); }
    .panel-hdr { padding: 12px 14px; font-size: .76rem; font-weight: 800;
                 color: var(--muted); border-bottom: 1px solid var(--border);
                 text-transform: uppercase; letter-spacing: .08em; flex-shrink: 0;
                 background: var(--surface); }
    .audit-list { flex: 1; overflow-y: auto; padding: 10px; display: flex;
                  flex-direction: column; gap: 8px; }
    .ae { background: var(--surface-soft); border-radius: var(--radius); padding: 9px 10px;
          font-size: .72rem; border-left: 4px solid var(--muted); }
    .ae.r-read   { border-color: var(--green); }
    .ae.r-low    { border-color: #84cc16; }
    .ae.r-medium { border-color: var(--orange); }
    .ae.r-high   { border-color: var(--red); }
    .ae-tool { font-weight: 700; color: var(--ha-blue); }
    .ae-time { color: var(--muted); float: right; }
    .ae-line { color: var(--muted); margin-top: 3px;
               overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .refresh-btn { border: none; background: var(--surface); color: var(--ha-blue);
                   cursor: pointer; padding: 10px 14px; font-size: .78rem; font-weight: 700;
                   border-top: 1px solid var(--border); flex-shrink: 0; }
    .diag { border: 1px solid var(--border); border-radius: var(--radius);
            background: var(--surface); overflow: hidden; flex-shrink: 0; box-shadow: var(--shadow); }
    .diag-hdr { display: flex; align-items: center; justify-content: space-between;
                padding: 7px 10px; border-bottom: 1px solid var(--border);
                font-size: .72rem; color: var(--muted); text-transform: uppercase;
                letter-spacing: .05em; }
    .diag-btn { border: none; background: transparent; color: var(--ha-blue);
                cursor: pointer; font-size: .69rem; font-weight: 700; }
    .diag-body { font-family: ui-monospace,SFMono-Regular,Menlo,monospace;
                 font-size: .68rem; color: var(--muted); max-height: 140px;
                 overflow-y: auto; white-space: pre-wrap; line-height: 1.5;
                 padding: 8px 10px; background: var(--surface-soft); }
    .diag.hidden .diag-body { display: none; }
    .diag.hidden .diag-hdr { border-bottom: none; }
    @media (max-width: 820px) {
      header { min-height: 52px; padding: 0 12px; }
      header h1 { font-size: .94rem; }
      .tabs { padding: 0 6px; }
      .tab-btn { flex: 1; padding-left: 6px; padding-right: 6px; min-width: 0; }
      .layout { padding: 10px; }
      .audit { display: none; }
      .msg { max-width: 94%; }
      .badges { gap: 5px; }
      .badge { padding: 4px 7px; font-size: .66rem; }
    }
    /* ── auth tab ── */
    #tab-auth { flex-direction: column; overflow-y: auto; padding: 16px; gap: 16px; }
    .auth-section { background: var(--surface); border: 1px solid var(--border);
                    border-radius: var(--radius); padding: 16px; display: flex;
                    flex-direction: column; gap: 12px; box-shadow: var(--shadow); }
    .auth-section h3 { font-size: .95rem; font-weight: 700; color: var(--text);
                        margin-bottom: 4px; }
    .auth-row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    .auth-row label { font-size: .8rem; color: var(--muted); min-width: 80px; }
    .auth-input { flex: 1; background: var(--input-bg); border: 1px solid var(--border);
                  border-radius: var(--radius); color: var(--text); padding: 9px 11px;
                  font-size: .85rem; font-family: inherit; min-width: 180px; }
    .auth-input:focus { outline: none; border-color: var(--ha-blue); box-shadow: 0 0 0 3px rgba(3,169,244,.16); }
    .auth-btn { background: var(--ha-blue); color: #fff; border: none;
                border-radius: var(--radius); padding: 9px 16px; font-weight: 700;
                font-size: .82rem; cursor: pointer; white-space: nowrap; }
    .auth-btn:hover { background: var(--ha-blue-dark); }
    .auth-btn.danger { background: #fdecea; color: #b3261e; }
    .auth-btn.danger:hover { background: var(--red); color: #fff; }
    .auth-btn.secondary { background: var(--surface-soft); color: var(--text); border: 1px solid var(--border); }
    .auth-btn.secondary:hover { background: var(--border); }
    .auth-result { font-size: .78rem; padding: 5px 9px; border-radius: 5px;
                   display: none; }
    .auth-result.ok  { background: rgba(15,157,88,.14); color: var(--green); display: block; }
    .auth-result.err { background: rgba(219,68,55,.14); color: var(--red); display: block; }
    .auth-result.info { background: rgba(3,169,244,.14); color: var(--ha-blue); display: block; }
    .auth-url-box { background: var(--surface-soft); border: 1px solid var(--border);
                    border-radius: var(--radius); padding: 10px 11px; font-size: .75rem;
                    font-family: ui-monospace,monospace; word-break: break-all;
                    color: var(--ha-blue); display: none; }
    /* ── profiles table ── */
    .prof-table { width: 100%; border-collapse: collapse; font-size: .78rem; }
    .prof-table th { text-align: left; color: var(--muted); font-weight: 600;
                     padding: 5px 8px; border-bottom: 1px solid var(--border); }
    .prof-table td { padding: 8px; border-bottom: 1px solid var(--border);
                     vertical-align: middle; }
    .type-badge { padding: 2px 7px; border-radius: 999px; font-size: .68rem;
                  font-weight: 700; }
    .type-api_key { background:#0c4a6e; color:#7dd3fc; }
    .type-token   { background:#4c1d95; color:#c4b5fd; }
    .type-oauth   { background:#14532d; color:#86efac; }
    .expired-badge { color: var(--red); font-size: .68rem; font-weight: 700; }
  </style>
</head>
<body>
  <header>
    <h1>&#129302; HA AI Operator</h1>
    <div class="badges" id="badges">
      <span class="badge badge-prov" id="badge-prov">loading…</span>
    </div>
  </header>
  <nav class="tabs">
    <button class="tab-btn active" onclick="switchTab('chat')">Chat</button>
    <button class="tab-btn" onclick="switchTab('audit')">Audit</button>
    <button class="tab-btn" onclick="switchTab('auth')">Auth</button>
  </nav>

  <!-- ── Chat tab ── -->
  <div id="tab-chat" class="tab-content active">
    <div class="layout">
      <div class="chat">
        <div class="messages" id="messages">
          <div class="msg system">Welcome to HA AI Operator. Loading configuration…</div>
        </div>
        <div class="typing" id="typing" style="display:none">Agent is thinking…</div>
        <div class="input-row">
          <textarea id="input" rows="2"
            placeholder="Ask about your home…"></textarea>
          <button class="send" id="sendBtn" onclick="send()">Send</button>
        </div>
        <div class="diag" id="diag">
          <div class="diag-hdr">
            <span>Diagnostics</span>
            <button class="diag-btn" id="diagToggle" type="button" onclick="toggleDiag()">Hide</button>
          </div>
          <div class="diag-body" id="diagBody">Initializing diagnostics…</div>
        </div>
      </div>
      <div class="audit">
        <div class="panel-hdr">Audit Log</div>
        <div class="audit-list" id="auditList">
          <div class="ae" style="border:none;color:var(--muted)">No actions yet.</div>
        </div>
        <button class="refresh-btn" onclick="loadAudit()">&#8635; Refresh</button>
      </div>
    </div>
  </div>

  <!-- ── Audit tab (full-page) ── -->
  <div id="tab-audit" class="tab-content">
    <div style="flex:1;display:flex;flex-direction:column;overflow:hidden;">
      <div class="panel-hdr" style="padding:14px 18px;">Audit Log</div>
      <div class="audit-list" id="auditListFull" style="flex:1;overflow-y:auto;padding:12px;gap:6px;">
        <div class="ae" style="border:none;color:var(--muted)">No actions yet.</div>
      </div>
      <button class="refresh-btn" onclick="loadAuditFull()">&#8635; Refresh</button>
    </div>
  </div>

  <!-- ── Auth tab ── -->
  <div id="tab-auth" class="tab-content">

    <!-- OpenAI Codex OAuth -->
    <div class="auth-section">
      <h3>OpenAI Codex — OAuth (PKCE)</h3>
      <ol style="font-size:.8rem;color:var(--muted);line-height:1.9;padding-left:1.2em;">
        <li>Click <strong style="color:var(--text)">Start Login</strong> — a login link appears below.</li>
        <li>Open the link in your browser and log in at OpenAI.</li>
        <li>After login, the browser tries to open <code style="background:#0f172a;padding:1px 5px;border-radius:3px">http://localhost:1455/auth/callback?code=…</code><br>
            The page will show <strong style="color:var(--orange)">ERR_CONNECTION_REFUSED</strong> — this is <em>normal and expected</em>.</li>
        <li><strong style="color:var(--text)">Copy the full URL</strong> from the browser's address bar (it contains <code style="background:#0f172a;padding:1px 4px;border-radius:3px">?code=…</code>).</li>
        <li>Paste it into the field below and click <strong style="color:var(--text)">Submit Code</strong>.</li>
      </ol>
      <div class="auth-row">
        <button class="auth-btn" onclick="startOAuth()">Start Login</button>
        <span id="oauth-status-text" style="font-size:.78rem;color:var(--muted)"></span>
      </div>
      <div class="auth-url-box" id="oauth-url-box"></div>
      <div class="auth-row" style="gap:8px;flex-wrap:wrap;">
        <input id="oauth-code-input" class="auth-input"
               placeholder="Paste full redirect URL from browser address bar here" />
        <button class="auth-btn secondary" onclick="completeOAuth()">Submit Code</button>
      </div>
      <div class="auth-result" id="oauth-result"></div>
    </div>

    <!-- Profiles -->
    <div class="auth-section">
      <h3>Saved Profiles <button class="auth-btn secondary" style="margin-left:8px;padding:4px 10px;font-size:.72rem;" onclick="loadProfiles()">&#8635; Refresh</button></h3>
      <div id="profiles-container">
        <span style="font-size:.8rem;color:var(--muted)">Loading…</span>
      </div>
    </div>

  </div>

  <script>
    const $ = id => document.getElementById(id);
    const msgs = $('messages');
    const input = $('input');
    const sendBtn = $('sendBtn');
    const diag = $('diag');
    const diagBody = $('diagBody');
    const diagToggle = $('diagToggle');
    let history = [];
    let diagHidden = false;
    let reqCounter = 0;
    let lastAuditError = '';
    const uiSessionId = Math.random().toString(36).slice(2, 10);

    function clip(value, maxLen = 240) {
      const text = String(value === undefined || value === null ? '' : value);
      return text.length <= maxLen ? text : text.slice(0, maxLen) + '...[truncated]';
    }
    function safeStringify(value) {
      try {
        return JSON.stringify(value);
      } catch (_e) {
        return '[unserializable]';
      }
    }
    function nextRequestId(scope) {
      reqCounter += 1;
      return 'ui-' + uiSessionId + '-' + scope + '-' + reqCounter;
    }

    // ── URL helpers ──────────────────────────────────────────────────────────
    function detectApiBase() {
      const p = window.location.pathname || '/';
      const ingress = p.match(/^\\/api\\/hassio_ingress\\/([^/]+)/);
      if (ingress) {
        return {base: '/api/hassio_ingress/' + ingress[1] + '/', reason: 'ingress_path'};
      }
      const slugOnly = p.match(/^\\/([^/]+)\\/?$/);
      if (slugOnly) {
        return {base: '/api/hassio_ingress/' + slugOnly[1] + '/', reason: 'slug_path'};
      }
      if (p === '/') return {base: '/', reason: 'root'};
      return {base: p.endsWith('/') ? p : p + '/', reason: 'fallback_path'};
    }
    const apiDetect = detectApiBase();
    const apiBase = apiDetect.base;
    const apiUrl = path => apiBase + String(path).replace(/^\\/+/, '');

    // ── Tab switching ─────────────────────────────────────────────────────────
    function switchTab(name) {
      document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
      document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
      const content = $('tab-' + name);
      if (content) content.classList.add('active');
      const btns = document.querySelectorAll('.tab-btn');
      btns.forEach(b => { if (b.textContent.trim().toLowerCase() === name) b.classList.add('active'); });
      if (name === 'auth') loadProfiles();
      if (name === 'audit') loadAuditFull();
    }

    function appendDiagLine(line) {
      if (!diagBody) return;
      const hadDefault = diagBody.textContent === 'Initializing diagnostics…';
      if (hadDefault) diagBody.textContent = '';
      diagBody.textContent += (diagBody.textContent ? '\\n' : '') + line;
      const lines = diagBody.textContent.split('\\n');
      if (lines.length > 120) {
        diagBody.textContent = lines.slice(lines.length - 120).join('\\n');
      }
      diagBody.scrollTop = diagBody.scrollHeight;
    }
    function toggleDiag() {
      if (!diag || !diagToggle) return;
      diagHidden = !diagHidden;
      diag.classList.toggle('hidden', diagHidden);
      diagToggle.textContent = diagHidden ? 'Show' : 'Hide';
    }
    function emitDiag(level, event, message, context) {
      const ts = new Date().toISOString();
      const line = '[' + ts + '] [' + String(level || 'info').toUpperCase() + '] ' +
        clip(event, 64) + ' - ' + clip(message || '', 260) +
        (context && Object.keys(context).length ? ' | ' + clip(safeStringify(context), 280) : '');
      appendDiagLine(line);
      if (level === 'error') {
        console.error('[HA-AI-UI]', event, message || '', context || {});
      } else if (level === 'warn' || level === 'warning') {
        console.warn('[HA-AI-UI]', event, message || '', context || {});
      } else {
        console.log('[HA-AI-UI]', event, message || '', context || {});
      }
    }
    async function sendFrontendLog(level, event, message, context = {}) {
      const payload = {
        level: level || 'info',
        event: event || 'ui.event',
        message: message || '',
        context,
        ts: new Date().toISOString(),
        url: window.location.href,
        session_id: uiSessionId
      };
      try {
        await fetch(apiUrl('api/frontend-log'), {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-Request-ID': nextRequestId('frontend-log')
          },
          body: JSON.stringify(payload),
          keepalive: true
        });
      } catch (_e) {
        // Never recurse logging if the log endpoint itself fails.
      }
    }
    function logEvent(level, event, message, context = {}) {
      emitDiag(level, event, message, context);
      sendFrontendLog(level, event, message, context);
    }

    function esc(t) {
      return String(t)
        .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    }
    function fmt(t) {
      return esc(t)
        .replace(/`([^`\\n]+)`/g, '<code>$1</code>')
        .replace(/\\*\\*([^*\\n]+)\\*\\*/g, '<strong>$1</strong>');
    }

    function addMsg(role, content) {
      const d = document.createElement('div');
      d.className = 'msg ' + role;
      d.innerHTML = fmt(content);
      msgs.appendChild(d);
      msgs.scrollTop = msgs.scrollHeight;
    }

    async function fetchWithTrace(label, path, options = {}, timeoutMs = 10000) {
      const reqId = nextRequestId(String(label || 'req').replace(/[^a-z0-9]/gi, '').slice(0, 14) || 'req');
      const url = apiUrl(path);
      const started = performance.now();
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
      const headers = Object.assign({}, options.headers || {}, {'X-Request-ID': reqId});
      logEvent('info', label + '.start', 'Fetching ' + url, {reqId, timeoutMs, apiBase});
      try {
        const response = await fetch(url, Object.assign({}, options, {headers, signal: controller.signal}));
        const durationMs = Math.round((performance.now() - started) * 10) / 10;
        logEvent('info', label + '.response', 'HTTP ' + response.status, {
          reqId,
          durationMs,
          ok: response.ok
        });
        return {response, reqId, url, durationMs};
      } catch (e) {
        const durationMs = Math.round((performance.now() - started) * 10) / 10;
        const timeout = e && e.name === 'AbortError';
        logEvent('error', label + '.network_error', timeout ? 'Request timed out' : (e.message || String(e)), {
          reqId,
          durationMs,
          timeout,
          url
        });
        throw e;
      } finally {
        clearTimeout(timeoutId);
      }
    }

    async function send() {
      const text = input.value.trim();
      if (!text) return;
      input.value = '';
      sendBtn.disabled = true;
      $('typing').style.display = 'block';
      addMsg('user', text);
      history.push({role:'user', content:text});
      try {
        const req = await fetchWithTrace('chat.completions', 'v1/chat/completions', {
          method: 'POST',
          headers: {'Content-Type':'application/json'},
          body: JSON.stringify({model:'ha-agent', messages:history, temperature:0.7})
        }, 60000);
        if (!req.response.ok) {
          throw new Error('HTTP ' + req.response.status + ' @ ' + req.url + ' req=' + req.reqId);
        }
        const d = await req.response.json();
        const reply = d.choices[0].message.content;
        addMsg('assistant', reply);
        history.push({role:'assistant', content:reply});
        logEvent('info', 'chat.success', 'Assistant response received', {
          reqId: req.reqId,
          replyChars: String(reply || '').length
        });
        setTimeout(loadAudit, 600);
      } catch(e) {
        const msg = e && e.message ? e.message : String(e);
        logEvent('error', 'chat.failure', msg, {historyLen: history.length});
        addMsg('system', 'Error: ' + msg);
      } finally {
        sendBtn.disabled = false;
        $('typing').style.display = 'none';
        input.focus();
      }
    }

    async function loadStatus() {
      const statusMsg = msgs.querySelector('.msg.system');
      try {
        const req = await fetchWithTrace('health', 'health', {}, 10000);
        if (!req.response.ok) {
          throw new Error('HTTP ' + req.response.status + ' @ ' + req.url + ' req=' + req.reqId);
        }
        const d = await req.response.json();
        $('badges').innerHTML =
          '<span class="badge badge-' + esc(d.mode) + '">' + esc(d.mode).toUpperCase() + '</span>' +
          '<span class="badge badge-prov">' + esc(d.llm_provider) + '</span>' +
          (d.allow_supervisor_api === 'true' ? '<span class="badge badge-sup">SUPERVISOR</span>' : '');
        if (statusMsg) statusMsg.textContent =
          'Mode: ' + d.mode + ' | Provider: ' + d.llm_provider +
          ' | Model: ' + (d.llm_model || '<unset>') +
          ' | Confirmation: ' + d.confirmation_required +
          ' | Max actions: ' + d.max_actions_per_turn;
        logEvent('info', 'health.loaded', 'Configuration loaded', {
          reqId: req.reqId,
          mode: d.mode,
          provider: d.llm_provider,
          model: d.llm_model || '',
          model_set: !!d.llm_model_set,
          app_log_level: d.app_log_level || 'n/a'
        });
      } catch(e) {
        const msg = e && e.message ? e.message : String(e);
        if (statusMsg) statusMsg.textContent = 'Could not reach add-on backend: ' + msg +
          ' — check the add-on log.';
        $('badges').innerHTML = '<span class="badge" style="background:#7f1d1d;color:#fca5a5">OFFLINE</span>';
        logEvent('error', 'health.failure', msg, {
          pathname: window.location.pathname,
          href: window.location.href,
          apiBase,
          detectReason: apiDetect.reason
        });
      }
    }

    async function loadAudit() {
      const reqId = nextRequestId('audit');
      const url = apiUrl('api/audit?limit=50');
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), 8000);
      try {
        const r = await fetch(url, {
          headers: {'X-Request-ID': reqId},
          signal: controller.signal
        });
        if (!r.ok) return;
        const d = await r.json();
        if (!d.entries || !d.entries.length) return;
        $('auditList').innerHTML = d.entries.map(e => {
          const t = e.timestamp
            ? new Date(e.timestamp).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'})
            : '';
          const confirmed = e.confirmed ? ' &#10003;' : '';
          return '<div class="ae r-' + esc(e.risk||'read') + '">' +
            '<span class="ae-tool">' + esc(e.tool) + confirmed + '</span>' +
            '<span class="ae-time">' + t + '</span>' +
            '<div class="ae-line">' + esc(e.params_summary||'') + '</div>' +
            '<div class="ae-line" style="color:#4b5563">' + esc(e.result_summary||'') + '</div>' +
            '</div>';
        }).join('');
        lastAuditError = '';
      } catch(e) {
        const msg = e && e.message ? e.message : String(e);
        if (msg !== lastAuditError) {
          lastAuditError = msg;
          logEvent('warn', 'audit.failure', msg, {reqId, url});
        }
      } finally {
        clearTimeout(timeoutId);
      }
    }

    async function loadAuditFull() {
      const url = apiUrl('api/audit?limit=200');
      try {
        const r = await fetch(url);
        if (!r.ok) return;
        const d = await r.json();
        const list = $('auditListFull');
        if (!list) return;
        if (!d.entries || !d.entries.length) {
          list.innerHTML = '<div class="ae" style="border:none;color:var(--muted)">No actions yet.</div>';
          return;
        }
        list.innerHTML = d.entries.map(e => {
          const t = e.timestamp
            ? new Date(e.timestamp).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'})
            : '';
          const confirmed = e.confirmed ? ' &#10003;' : '';
          return '<div class="ae r-' + esc(e.risk||'read') + '">' +
            '<span class="ae-tool">' + esc(e.tool) + confirmed + '</span>' +
            '<span class="ae-time">' + t + '</span>' +
            '<div class="ae-line">' + esc(e.params_summary||'') + '</div>' +
            '<div class="ae-line" style="color:#4b5563">' + esc(e.result_summary||'') + '</div>' +
            '</div>';
        }).join('');
      } catch(_e) {}
    }

    // ── Auth tab functions ────────────────────────────────────────────────────

    function showAuthResult(elemId, ok, msg) {
      const el = $(elemId);
      if (!el) return;
      el.className = 'auth-result ' + (ok === null ? 'info' : ok ? 'ok' : 'err');
      el.textContent = msg;
    }

    async function startOAuth() {
      showAuthResult('oauth-result', null, 'Starting OAuth flow…');
      $('oauth-url-box').style.display = 'none';
      $('oauth-status-text').textContent = '';
      try {
        const r = await fetch(apiUrl('oauth/openai-codex/start'), {method: 'POST'});
        const d = await r.json();
        if (!r.ok) { showAuthResult('oauth-result', false, d.detail || 'Start failed'); return; }
        const urlBox = $('oauth-url-box');
        urlBox.style.display = 'block';
        urlBox.innerHTML = '<strong style="color:var(--text)">Step 2:</strong> ' +
          '<a href="' + esc(d.auth_url) + '" target="_blank" rel="noopener" style="color:#7dd3fc">Open this login link in your browser</a>' +
          '<br><small style="color:var(--muted);word-break:break-all">' + esc(d.auth_url) + '</small>';
        $('oauth-status-text').textContent = 'Login link ready.';
        showAuthResult('oauth-result', null,
          'After login: copy the full URL from your browser address bar (even if it shows an error page), then paste it below.');
      } catch(e) {
        showAuthResult('oauth-result', false, 'Network error: ' + (e.message || String(e)));
      }
    }

    async function completeOAuth() {
      const codeInput = ($('oauth-code-input').value || '').trim();
      if (!codeInput) {
        showAuthResult('oauth-result', false, 'Please paste the redirect URL from your browser address bar first.');
        return;
      }
      showAuthResult('oauth-result', null, 'Exchanging tokens with OpenAI…');
      try {
        const r = await fetch(apiUrl('oauth/openai-codex/complete'), {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({input: codeInput})
        });
        const d = await r.json();
        if (!r.ok) {
          showAuthResult('oauth-result', false, d.detail || 'Token exchange failed');
          return;
        }
        showAuthResult('oauth-result', true,
          'Saved! Profile ' + esc(d.profileId) +
          (d.expiresIso ? ' — expires ' + esc(d.expiresIso) : ''));
        $('oauth-code-input').value = '';
        loadProfiles();
      } catch(e) {
        showAuthResult('oauth-result', false, 'Network error: ' + (e.message || String(e)));
      }
    }

    async function loadProfiles() {
      const container = $('profiles-container');
      if (!container) return;
      try {
        const r = await fetch(apiUrl('auth/status'));
        const d = await r.json();
        if (!d.profiles || !d.profiles.length) {
          container.innerHTML = '<span style="font-size:.8rem;color:var(--muted)">No profiles saved yet.</span>';
          return;
        }
        container.innerHTML = '<table class="prof-table"><thead><tr>' +
          '<th>ID</th><th>Type</th><th>Provider</th><th>Expires</th><th>Errors</th><th>Actions</th>' +
          '</tr></thead><tbody>' +
          d.profiles.map(p => {
            const expiredBadge = p.isExpired ? '<span class="expired-badge"> EXPIRED</span>' : '';
            const expiryStr = p.expiresIso
              ? new Date(p.expiresIso).toLocaleString([], {dateStyle:'short',timeStyle:'short'}) + (p.isExpired ? '' : '')
              : '—';
            return '<tr>' +
              '<td><code style="font-size:.72rem">' + esc(p.profileId) + '</code></td>' +
              '<td><span class="type-badge type-' + esc(p.type) + '">' + esc(p.type) + '</span></td>' +
              '<td>' + esc(p.provider) + '</td>' +
              '<td>' + esc(expiryStr) + expiredBadge + '</td>' +
              '<td>' + esc(String(p.errorCount || 0)) + '</td>' +
              '<td style="display:flex;gap:5px;">' +
              '<button class="auth-btn secondary" style="padding:3px 9px;font-size:.7rem" onclick="testProfile(' + "'" + esc(p.profileId) + "'" + ')">Test</button>' +
              '<button class="auth-btn danger" style="padding:3px 9px;font-size:.7rem" onclick="deleteProfile(' + "'" + esc(p.profileId) + "'" + ')">Delete</button>' +
              '</td></tr>';
          }).join('') +
          '</tbody></table>';
      } catch(e) {
        container.innerHTML = '<span style="font-size:.8rem;color:var(--red)">Error loading profiles: ' + esc(e.message || String(e)) + '</span>';
      }
    }

    async function testProfile(id) {
      try {
        const r = await fetch(apiUrl('auth/test/' + encodeURIComponent(id)), {method: 'POST'});
        const d = await r.json();
        alert((d.ok ? '✓ ' : '✗ ') + (d.detail || (d.ok ? 'OK' : 'Failed')));
      } catch(e) {
        alert('Error: ' + (e.message || String(e)));
      }
    }

    async function deleteProfile(id) {
      if (!confirm('Delete profile ' + id + '?')) return;
      try {
        const r = await fetch(apiUrl('auth/profile/' + encodeURIComponent(id)), {method: 'DELETE'});
        const d = await r.json();
        if (r.ok) loadProfiles();
        else alert('Error: ' + (d.detail || 'Delete failed'));
      } catch(e) {
        alert('Error: ' + (e.message || String(e)));
      }
    }

    input.addEventListener('keydown', e => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
    });
    window.addEventListener('error', e => {
      logEvent('error', 'window.error', e.message || 'Unhandled JS error', {
        source: e.filename,
        line: e.lineno,
        column: e.colno
      });
    });
    window.addEventListener('unhandledrejection', e => {
      const reason = e.reason && e.reason.message ? e.reason.message : String(e.reason);
      logEvent('error', 'window.unhandledrejection', reason, {});
    });

    logEvent('info', 'bootstrap.start', 'UI boot', {
      href: window.location.href,
      pathname: window.location.pathname,
      apiBase,
      detectReason: apiDetect.reason,
      session: uiSessionId
    });
    loadStatus();
    loadAudit();
    setInterval(loadAudit, 30000);
  </script>
</body>
</html>
"""
