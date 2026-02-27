"""Agent loop for HA AI Operator.

Flow
────
1.  Receive ChatCompletionRequest.
2.  Check whether the last user message is a CONFIRM:<token> reply.
    If yes → load pending plan → execute it → return summary.
3.  Otherwise:
    a. Build messages with system prompt.
    b. Call upstream LLM (OpenAI-compatible tool-calling).
    c. For each batch of tool calls returned:
       - classify risk via PolicyEngine
       - if any action requires confirmation → persist plan → ask user
       - otherwise execute immediately and continue loop
    d. Repeat until LLM signals stop or max_actions reached.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from typing import Any, Optional

import httpx

import ha_client
from llm_clients import BaseLLMClient, make_llm_client
from policy import PolicyEngine, classify_risk, is_supervisor_tool
from schemas import (
    AuditLogEntry,
    ChatCompletionRequest,
    ChatMessage,
    PlannedAction,
    RiskLevel,
    Role,
)
from storage import (
    append_audit,
    delete_pending_plan,
    get_pending_plan,
    load_soul,
    save_pending_plan,
)

# ── System prompt construction ────────────────────────────────────────────────
# The system prompt is assembled from three layers (outermost first):
#   1. Soul  — loaded from /data/soul.md (user) or /app/default_soul.md (default)
#   2. Mode  — one-liner injected per operating mode
#   3. Runtime constraints — confirmation and supervisor flags

_MODE_NOTES = {
    "read_only": (
        "## Current operating mode: read_only\n"
        "You may ONLY read data from Home Assistant (ha_get_state, ha_list_entities, "
        "ha_get_config). Service calls are blocked at the policy layer and you must "
        "not attempt them."
    ),
    "control_assist": (
        "## Current operating mode: control_assist\n"
        "You may perform low-risk control actions: lights, media players, input "
        "helpers. Medium and high-risk actions (security, locks, alarms) are blocked."
    ),
    "ops_write": (
        "## Current operating mode: ops_write\n"
        "You have full operator access. Any medium or high-risk action will be "
        "presented as a plan requiring a CONFIRM token before execution."
    ),
}

# ── Tool definitions (sent to upstream LLM) ───────────────────────────────────

_TOOLS_BASE: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "ha_get_state",
            "description": "Get the current state of a single Home Assistant entity.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {
                        "type": "string",
                        "description": "Full entity ID, e.g. 'light.living_room'.",
                    }
                },
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ha_list_entities",
            "description": (
                "List Home Assistant entities. "
                "Optionally filter by domain (e.g. 'light', 'switch', 'sensor')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "domain": {
                        "type": "string",
                        "description": "Optional domain filter.",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ha_get_config",
            "description": "Return the Home Assistant core configuration.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ha_call_service",
            "description": (
                "Call any Home Assistant service. "
                "Only available in control_assist or ops_write mode."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "domain": {
                        "type": "string",
                        "description": "Service domain, e.g. 'light'.",
                    },
                    "service": {
                        "type": "string",
                        "description": "Service name, e.g. 'turn_on'.",
                    },
                    "service_data": {
                        "type": "object",
                        "description": "Payload for the service call.",
                    },
                },
                "required": ["domain", "service", "service_data"],
            },
        },
    },
]

_TOOLS_SUPERVISOR: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "supervisor_host_info",
            "description": "Get host hardware/OS information from the Supervisor.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "supervisor_core_info",
            "description": "Get HA Core version/state from the Supervisor.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "supervisor_restart_core",
            "description": (
                "VERY HIGH RISK — Restart the Home Assistant Core via Supervisor. "
                "This causes downtime. Always require explicit user confirmation."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]


# ── Agent ─────────────────────────────────────────────────────────────────────

class Agent:
    def __init__(self) -> None:
        self._policy = PolicyEngine()
        self._llm: BaseLLMClient = make_llm_client()
        self._mode = os.environ.get("MODE", "read_only")
        self._allow_sup = os.environ.get("ALLOW_SUPERVISOR_API", "false").lower() == "true"

    # ── helpers ───────────────────────────────────────────────────────────────

    def _tools(self) -> list[dict]:
        t = list(_TOOLS_BASE)
        if self._allow_sup:
            t.extend(_TOOLS_SUPERVISOR)
        # In read_only mode, drop service-call tools to save context window space.
        if self._mode == "read_only":
            t = [x for x in t if x["function"]["name"] != "ha_call_service"]
        return t

    def _system_prompt(self) -> str:
        """Assemble system prompt: soul + mode note + runtime flags."""
        soul = load_soul()  # /data/soul.md → /app/default_soul.md → ""
        mode_note = _MODE_NOTES.get(self._mode, f"## Mode: {self._mode}")
        sup_note = (
            "## Supervisor API\nSupervisor tools (host info, core info, restart) are ENABLED."
            if self._allow_sup
            else ""
        )
        return "\n\n---\n\n".join(filter(None, [soul, mode_note, sup_note]))

    def _confirmation_token(self, plan_data: dict) -> str:
        raw = json.dumps(plan_data, sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()[:20]

    def _audit(
        self,
        tool: str,
        params: dict,
        result: str,
        risk: RiskLevel,
        confirmed: bool = False,
    ) -> None:
        safe_params = {
            k: v
            for k, v in params.items()
            if k.lower() not in ("api_key", "token", "password", "secret")
        }
        entry = AuditLogEntry(
            timestamp=datetime.utcnow(),
            actor="agent",
            mode=self._mode,
            tool=tool,
            params_summary=json.dumps(safe_params, default=str)[:300],
            result_summary=result[:300],
            risk=risk.value,
            confirmed=confirmed,
        )
        append_audit(entry)

    # ── tool executor ─────────────────────────────────────────────────────────

    async def _run_tool(
        self, tool: str, args: dict
    ) -> tuple[str, RiskLevel, bool]:
        """Execute *tool* with *args*.  Returns (result_str, risk, success)."""
        risk = classify_risk(tool, args)
        allowed, reason = self._policy.check_action(tool, args)
        if not allowed:
            return f"[DENIED] {reason}", risk, False

        try:
            match tool:
                case "ha_get_state":
                    data = await ha_client.ha_get_state(args["entity_id"])
                case "ha_list_entities":
                    data = await ha_client.ha_list_entities(args.get("domain"))
                    if isinstance(data, list) and len(data) > 50:
                        data = data[:50] + [{"_note": f"{len(data) - 50} more entities truncated"}]
                case "ha_get_config":
                    data = await ha_client.ha_get_config()
                case "ha_call_service":
                    data = await ha_client.ha_call_service(
                        args["domain"], args["service"], args.get("service_data", {})
                    )
                case "supervisor_host_info":
                    data = await ha_client.supervisor_host_info()
                case "supervisor_core_info":
                    data = await ha_client.supervisor_core_info()
                case "supervisor_restart_core":
                    data = await ha_client.supervisor_restart_core()
                case _:
                    return f"[ERROR] Unknown tool: {tool}", risk, False

            result = json.dumps(data, default=str)
            # Cap result length to keep context windows manageable.
            if len(result) > 3000:
                result = result[:3000] + "…[truncated]"
            return result, risk, True

        except httpx.HTTPStatusError as exc:
            return f"[HTTP {exc.response.status_code}] {exc.response.text[:200]}", risk, False
        except Exception as exc:
            return f"[ERROR] {type(exc).__name__}: {exc}", risk, False

    # ── confirmation execution ────────────────────────────────────────────────

    async def _execute_confirmed_plan(self, plan_data: dict) -> str:
        tool_calls: list[dict] = plan_data.get("tool_calls", [])
        lines: list[str] = ["**Executing confirmed plan:**\n"]
        for tc in tool_calls:
            tool = tc["tool"]
            args = tc["args"]
            result, risk, ok = await self._run_tool(tool, args)
            self._audit(tool, args, result, risk, confirmed=True)
            status = "✓" if ok else "✗"
            lines.append(f"- {status} **{tool}** → {result[:400]}")
        return "\n".join(lines)

    # ── main entry point ──────────────────────────────────────────────────────

    async def process(self, request: ChatCompletionRequest) -> str:
        # ── Check for CONFIRM reply ───────────────────────────────────────────
        last_user = next(
            (m.content or "" for m in reversed(request.messages) if m.role == Role.user),
            "",
        ).strip()

        if last_user.upper().startswith("CONFIRM:"):
            token = last_user.split(":", 1)[1].strip()
            plan_data = get_pending_plan(token)
            if plan_data:
                delete_pending_plan(token)
                return await self._execute_confirmed_plan(plan_data)
            return (
                "Confirmation token not found or expired. "
                "Please re-state your request so I can generate a new plan."
            )

        # ── Build initial message list ────────────────────────────────────────
        messages: list[dict] = [
            {"role": "system", "content": self._system_prompt()}
        ] + [
            {"role": m.role.value, "content": m.content or ""}
            for m in request.messages
        ]

        tools = self._tools()
        model = request.model or "ha-agent"
        temperature = request.temperature if request.temperature is not None else 0.7
        actions_taken = 0
        max_actions = self._policy.max_actions_count()

        # ── Agent loop ────────────────────────────────────────────────────────
        while actions_taken < max_actions:
            try:
                resp = await self._llm.chat(
                    messages=messages,
                    model=model,
                    temperature=temperature,
                    # Only supply tools if the LLM can act
                    tools=tools if self._mode != "read_only" else None,
                )
            except httpx.HTTPStatusError as exc:
                return (
                    f"LLM request failed (HTTP {exc.response.status_code}): "
                    f"{exc.response.text[:300]}"
                )
            except Exception as exc:
                return f"LLM error: {type(exc).__name__}: {exc}"

            choice = resp.get("choices", [{}])[0]
            msg = choice.get("message", {})
            finish = choice.get("finish_reason", "stop")
            tool_calls: list[dict] = msg.get("tool_calls") or []

            # No tool calls → final text answer
            if not tool_calls or finish == "stop":
                return msg.get("content") or "No response from agent."

            # ── Classify risk for the full batch ─────────────────────────────
            batch: list[dict] = []
            for tc in tool_calls:
                fn = tc.get("function", {})
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments", "{}"))
                except json.JSONDecodeError:
                    args = {}
                risk = classify_risk(name, args)
                batch.append(
                    {"tool_call_id": tc.get("id", ""), "tool": name, "args": args, "risk": risk.value}
                )

            # ── Confirmation gate ─────────────────────────────────────────────
            planned = [
                PlannedAction(tool=b["tool"], params=b["args"], risk=RiskLevel(b["risk"]))
                for b in batch
            ]
            if self._policy.plan_requires_confirmation(planned):
                plan_data = {
                    "tool_calls": batch,
                    "model": model,
                    "temperature": temperature,
                }
                token = self._confirmation_token(plan_data)
                save_pending_plan(token, plan_data)
                summary = "\n".join(
                    f"  {i+1}. **{b['tool']}**"
                    f"({json.dumps(b['args'], default=str)[:120]}) "
                    f"[risk: {b['risk']}]"
                    for i, b in enumerate(batch)
                )
                return (
                    f"I have prepared the following plan:\n\n{summary}\n\n"
                    f"**To confirm**, reply exactly:\n```\nCONFIRM:{token}\n```\n"
                    "Or describe a different approach to cancel."
                )

            # ── Execute the batch ─────────────────────────────────────────────
            messages.append(msg)  # append assistant message with tool_calls
            tool_results: list[dict] = []

            for b in batch:
                result, risk, _ok = await self._run_tool(b["tool"], b["args"])
                self._audit(b["tool"], b["args"], result, risk)
                tool_results.append(
                    {
                        "role": "tool",
                        "tool_call_id": b["tool_call_id"],
                        "content": result,
                    }
                )
                actions_taken += 1
                if actions_taken >= max_actions:
                    break

            messages.extend(tool_results)

        return (
            f"Reached the maximum of {max_actions} actions per turn. "
            "Ask me to continue if more steps are needed."
        )
