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

import os
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from agent import Agent
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


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "version": "0.1.0",
        "mode": os.environ.get("MODE", "unknown"),
        "llm_provider": os.environ.get("LLM_PROVIDER", "unknown"),
        "llm_base_url_set": bool(os.environ.get("LLM_BASE_URL", "")),
        "llm_api_key_set": bool(os.environ.get("LLM_API_KEY", "")),
        "allow_supervisor_api": os.environ.get("ALLOW_SUPERVISOR_API", "false"),
        "confirmation_required": os.environ.get("CONFIRMATION_REQUIRED", "true"),
        "max_actions_per_turn": os.environ.get("MAX_ACTIONS_PER_TURN", "5"),
        "audit_log_level": os.environ.get("AUDIT_LOG_LEVEL", "minimal"),
    }


# ── Chat completions ──────────────────────────────────────────────────────────

@app.post("/v1/chat/completions", response_model=ChatCompletionResponse)
async def chat_completions(req: ChatCompletionRequest) -> ChatCompletionResponse:
    agent = Agent()
    try:
        content = await agent.process(req)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Agent error: {exc}") from exc

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
async def get_audit(limit: int = 50) -> dict[str, Any]:
    limit = max(1, min(limit, 500))
    entries = read_audit(limit=limit)
    return {"entries": entries, "count": len(entries)}


# ── Self-test ─────────────────────────────────────────────────────────────────

@app.get("/debug/selftest")
async def selftest() -> dict[str, Any]:
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
    checks["llm_api_key_set"] = bool(os.environ.get("LLM_API_KEY", ""))
    checks["allow_supervisor_api"] = os.environ.get("ALLOW_SUPERVISOR_API", "false")

    all_ok: bool = bool(
        checks.get("supervisor_token_present") and checks.get("ha_api_reachable")
    )
    return {"ok": all_ok, "checks": checks}


# ── Ingress UI ────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def ui_root() -> HTMLResponse:
    return HTMLResponse(content=_UI_HTML)


# Catch any unknown sub-path so the Ingress panel doesn't 404 on reload.
@app.get("/{full_path:path}", response_class=HTMLResponse, include_in_schema=False)
async def ui_catchall(full_path: str) -> HTMLResponse:
    # Let actual API routes bubble up as 404; only serve UI for unknown paths.
    api_prefixes = ("v1/", "api/", "health", "debug/")
    if any(full_path.startswith(p) for p in api_prefixes):
        raise HTTPException(status_code=404, detail="Not found")
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
      --bg: #111827; --surface: #1f2937; --border: #374151;
      --accent: #38bdf8; --text: #e5e7eb; --muted: #6b7280;
      --green: #22c55e; --orange: #f97316; --red: #ef4444;
    }
    body { font-family: -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
           background: var(--bg); color: var(--text); height: 100vh;
           display: flex; flex-direction: column; overflow: hidden; }
    /* ── header ── */
    header { background: var(--surface); border-bottom: 1px solid var(--border);
             padding: 10px 18px; display: flex; align-items: center; gap: 12px;
             flex-shrink: 0; }
    header h1 { font-size: 1.05rem; font-weight: 700; color: var(--accent); }
    .badges { margin-left: auto; display: flex; gap: 6px; flex-wrap: wrap; }
    .badge { padding: 3px 9px; border-radius: 999px; font-size: .72rem;
             font-weight: 600; white-space: nowrap; }
    .badge-read_only    { background:#14532d; color:#86efac; }
    .badge-control_assist { background:#7c2d12; color:#fdba74; }
    .badge-ops_write    { background:#7f1d1d; color:#fca5a5; }
    .badge-sup  { background:#4c1d95; color:#c4b5fd; }
    .badge-prov { background:#0c4a6e; color:#7dd3fc; }
    /* ── main layout ── */
    .layout { display: flex; flex: 1; overflow: hidden; }
    /* ── chat panel ── */
    .chat { flex: 1; display: flex; flex-direction: column; padding: 14px;
            gap: 10px; min-width: 0; }
    .messages { flex: 1; overflow-y: auto; display: flex;
                flex-direction: column; gap: 10px; padding: 4px 0; }
    .msg { padding: 11px 15px; border-radius: 12px; max-width: 82%;
           line-height: 1.55; word-wrap: break-word; white-space: pre-wrap; }
    .msg.user      { background: #1e3a5f; align-self: flex-end; }
    .msg.assistant { background: var(--surface); align-self: flex-start;
                     border: 1px solid var(--border); }
    .msg.system    { background: transparent; align-self: center;
                     color: var(--muted); font-size: .8rem; font-style: italic; }
    .msg code   { background: #0f172a; padding: 1px 5px; border-radius: 4px;
                  font-family: monospace; font-size: .88em; }
    .msg strong { color: var(--accent); }
    .typing { color: var(--muted); font-style: italic; font-size: .82rem;
              padding: 2px 0; flex-shrink: 0; }
    .input-row { display: flex; gap: 8px; flex-shrink: 0; }
    textarea { flex: 1; background: var(--surface); border: 1px solid var(--border);
               border-radius: 8px; color: var(--text); padding: 9px 13px;
               font-size: .93rem; resize: none; font-family: inherit;
               min-height: 44px; max-height: 140px; }
    textarea:focus { outline: none; border-color: var(--accent); }
    .send { background: var(--accent); color: #0c1520; border: none;
            border-radius: 8px; padding: 9px 20px; font-weight: 700;
            cursor: pointer; align-self: flex-end; }
    .send:hover { background: #7dd3fc; }
    .send:disabled { background: var(--border); color: var(--muted);
                     cursor: not-allowed; }
    /* ── audit panel ── */
    .audit { width: 360px; background: var(--surface);
             border-left: 1px solid var(--border);
             display: flex; flex-direction: column; overflow: hidden; flex-shrink: 0; }
    .panel-hdr { padding: 10px 14px; font-size: .78rem; font-weight: 700;
                 color: var(--accent); border-bottom: 1px solid var(--border);
                 text-transform: uppercase; letter-spacing: .06em; flex-shrink: 0; }
    .audit-list { flex: 1; overflow-y: auto; padding: 8px; display: flex;
                  flex-direction: column; gap: 5px; }
    .ae { background: var(--bg); border-radius: 6px; padding: 7px 9px;
          font-size: .72rem; border-left: 3px solid var(--muted); }
    .ae.r-read   { border-color: var(--green); }
    .ae.r-low    { border-color: #84cc16; }
    .ae.r-medium { border-color: var(--orange); }
    .ae.r-high   { border-color: var(--red); }
    .ae-tool { font-weight: 700; color: var(--accent); }
    .ae-time { color: var(--muted); float: right; }
    .ae-line { color: var(--muted); margin-top: 3px;
               overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .refresh-btn { border: none; background: none; color: var(--accent);
                   cursor: pointer; padding: 8px 14px; font-size: .75rem;
                   border-top: 1px solid var(--border); flex-shrink: 0; }
    @media (max-width: 700px) { .audit { display: none; } }
  </style>
</head>
<body>
  <header>
    <h1>&#129302; HA AI Operator</h1>
    <div class="badges" id="badges">
      <span class="badge badge-prov" id="badge-prov">loading…</span>
    </div>
  </header>
  <div class="layout">
    <div class="chat">
      <div class="messages" id="messages">
        <div class="msg system">Welcome to HA AI Operator. Loading configuration…</div>
      </div>
      <div class="typing" id="typing" style="display:none">Agent is thinking…</div>
      <div class="input-row">
        <textarea id="input" rows="2"
          placeholder="Ask about your home or give instructions… (Enter to send, Shift+Enter for newline)"></textarea>
        <button class="send" id="sendBtn" onclick="send()">Send</button>
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
  <script>
    const $ = id => document.getElementById(id);
    const msgs = $('messages');
    const input = $('input');
    const sendBtn = $('sendBtn');
    let history = [];

    function esc(t) {
      return String(t)
        .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    }
    function fmt(t) {
      return esc(t)
        .replace(/`([^`\n]+)`/g, '<code>$1</code>')
        .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
    }

    function addMsg(role, content) {
      const d = document.createElement('div');
      d.className = 'msg ' + role;
      d.innerHTML = fmt(content);
      msgs.appendChild(d);
      msgs.scrollTop = msgs.scrollHeight;
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
        const r = await fetch('v1/chat/completions', {
          method: 'POST',
          headers: {'Content-Type':'application/json'},
          body: JSON.stringify({model:'ha-agent', messages:history, temperature:0.7})
        });
        if (!r.ok) throw new Error('HTTP ' + r.status);
        const d = await r.json();
        const reply = d.choices[0].message.content;
        addMsg('assistant', reply);
        history.push({role:'assistant', content:reply});
        setTimeout(loadAudit, 600);
      } catch(e) {
        addMsg('system', 'Error: ' + e.message);
      } finally {
        sendBtn.disabled = false;
        $('typing').style.display = 'none';
        input.focus();
      }
    }

    async function loadStatus() {
      try {
        const r = await fetch('health');
        const d = await r.json();
        const badgesEl = $('badges');
        badgesEl.innerHTML = `
          <span class="badge badge-${esc(d.mode)}">${esc(d.mode).toUpperCase()}</span>
          <span class="badge badge-prov">${esc(d.llm_provider)}</span>
          ${d.allow_supervisor_api === 'true'
            ? '<span class="badge badge-sup">SUPERVISOR</span>' : ''}
        `;
        // Update welcome message
        msgs.querySelector('.msg.system').textContent =
          `Mode: ${d.mode} | Provider: ${d.llm_provider} | ` +
          `Confirmation: ${d.confirmation_required} | ` +
          `Max actions: ${d.max_actions_per_turn}`;
      } catch(e) { /* ignore */ }
    }

    async function loadAudit() {
      try {
        const r = await fetch('api/audit?limit=50');
        const d = await r.json();
        if (!d.entries.length) return;
        $('auditList').innerHTML = d.entries.map(e => {
          const t = e.timestamp
            ? new Date(e.timestamp).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'})
            : '';
          const confirmed = e.confirmed ? ' &#10003;' : '';
          return `<div class="ae r-${esc(e.risk||'read')}">
            <span class="ae-tool">${esc(e.tool)}${confirmed}</span>
            <span class="ae-time">${t}</span>
            <div class="ae-line">${esc(e.params_summary||'')}</div>
            <div class="ae-line" style="color:#4b5563">${esc(e.result_summary||'')}</div>
          </div>`;
        }).join('');
      } catch(e) { /* ignore */ }
    }

    input.addEventListener('keydown', e => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
    });

    loadStatus();
    loadAudit();
    setInterval(loadAudit, 30000);
  </script>
</body>
</html>
"""
