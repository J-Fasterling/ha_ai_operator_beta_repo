"""LLM backend adapters.

All clients expose the same async interface:

    async def chat(
        messages: list[dict],   # OpenAI-format internally
        model: str,
        temperature: float,
        tools: list[dict] | None,
    ) -> dict                   # OpenAI-compatible response dict

Messages are always kept in OpenAI format inside the agent.  Each adapter
converts to/from its native wire format transparently, so the agent loop
never needs to know which backend is in use.

Supported backends
──────────────────
  openai_compatible  Any endpoint implementing POST /chat/completions
                     (OpenAI, Groq, Together, OpenRouter, LM Studio, …)
  ollama             Local Ollama – defaults to http://localhost:11434/v1
  custom_http        Alias for openai_compatible with an explicit base URL
  anthropic          Anthropic Messages API (claude-* models)
                     Uses x-api-key header and /v1/messages endpoint.

OAuth / Codex note
──────────────────
OpenAI-compatible providers default to static API keys.
For OpenAI, this add-on can also send a bearer OAuth token (Codex OAuth mode).
Codex OAuth tokens are ChatGPT consumer tokens — they lack the API scopes
required by ``api.openai.com`` endpoints.  The official Codex CLI sends them
to ``https://chatgpt.com/backend-api/codex/responses`` with a
``ChatGPT-Account-ID`` header.  When ``auth_mode == "codex_oauth"`` this
client reproduces that behaviour and converts between the Responses API
wire format and the OpenAI Chat Completions format transparently.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from abc import ABC, abstractmethod
from typing import Any, Optional

import httpx

log = logging.getLogger("ha_ai_operator.llm_clients")

# ── Base ──────────────────────────────────────────────────────────────────────

class BaseLLMClient(ABC):
    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, Any]],
        model: str,
        temperature: float,
        tools: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        """Return an OpenAI-compatible response dict."""


# ── OpenAI-compatible (covers OpenAI, Groq, Ollama, custom HTTP) ──────────────

_CODEX_CHATGPT_BASE = "https://chatgpt.com/backend-api/codex"


class OpenAICompatibleClient(BaseLLMClient):
    def __init__(
        self,
        provider: str,
        base_url: str,
        api_key: str,
        auth_mode: str = "api_key",
        oauth_token: str = "",
        account_id: str = "",
    ) -> None:
        self._provider = provider
        self._api_key = api_key
        self._auth_mode = auth_mode
        self._oauth_token = oauth_token
        self._account_id = account_id
        # Resolve base URL
        if base_url:
            self._base = base_url.rstrip("/")
        elif provider == "ollama":
            self._base = "http://localhost:11434/v1"
        else:
            self._base = "https://api.openai.com/v1"

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        auth_value = ""
        if self._auth_mode == "codex_oauth":
            auth_value = self._oauth_token or self._api_key
        else:
            auth_value = self._api_key or self._oauth_token
        if auth_value:
            h["Authorization"] = f"Bearer {auth_value}"
        if self._auth_mode == "codex_oauth" and self._account_id:
            h["ChatGPT-Account-ID"] = self._account_id
        return h

    # ── Responses API converters (Codex OAuth) ──────────────────────────────

    @staticmethod
    def _tools_to_responses(tools: list[dict]) -> list[dict]:
        """Convert OpenAI Chat Completions tool defs → Responses API format.

        Chat Completions:
            {"type": "function", "function": {"name": …, "parameters": …}}
        Responses API:
            {"type": "function", "name": …, "parameters": …}
        """
        result = []
        for t in tools:
            fn = t.get("function", {})
            result.append({
                "type": "function",
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters", {"type": "object", "properties": {}}),
            })
        return result

    @staticmethod
    def _messages_to_responses(
        messages: list[dict],
    ) -> tuple[str, list[dict]]:
        """Convert OpenAI Chat Completions messages → Responses API input items.

        Returns (instructions, input_items).
        - system messages → concatenated into instructions string
        - user messages → {"role": "user", "content": "…"}
        - assistant messages (text only) → {"role": "assistant", "content": "…"}
        - assistant messages with tool_calls → function_call items
        - tool result messages → function_call_output items
        """
        instructions_parts: list[str] = []
        items: list[dict] = []

        for msg in messages:
            role = msg.get("role", "")

            if role == "system":
                if msg.get("content"):
                    instructions_parts.append(msg["content"])
                continue

            if role == "user":
                items.append({"role": "user", "content": msg.get("content", "")})
                continue

            if role == "assistant":
                tool_calls: list[dict] = msg.get("tool_calls") or []
                text: str = msg.get("content") or ""
                if tool_calls:
                    # Emit text as a message item first, then each tool call
                    if text:
                        items.append({"role": "assistant", "content": text})
                    for tc in tool_calls:
                        fn = tc.get("function", {})
                        items.append({
                            "type": "function_call",
                            "call_id": tc.get("id", ""),
                            "name": fn.get("name", ""),
                            "arguments": fn.get("arguments", "{}"),
                        })
                else:
                    items.append({"role": "assistant", "content": text or ""})
                continue

            if role == "tool":
                items.append({
                    "type": "function_call_output",
                    "call_id": msg.get("tool_call_id", ""),
                    "output": msg.get("content", ""),
                })
                continue

        return "\n\n".join(instructions_parts), items

    @staticmethod
    def _responses_to_openai(raw: dict) -> dict:
        """Convert a Responses API response → OpenAI Chat Completions format.

        Responses API output items:
        - {"type": "message", "content": [{"type": "output_text", "text": …}]}
        - {"type": "function_call", "call_id": …, "name": …, "arguments": …}

        Mapped to:
        - choices[0].message.content
        - choices[0].message.tool_calls[]
        """
        output_items: list[dict] = raw.get("output", [])
        status: str = raw.get("status", "completed")

        text_parts: list[str] = []
        tool_calls: list[dict] = []

        for item in output_items:
            item_type = item.get("type", "")

            if item_type == "message":
                for block in item.get("content", []):
                    if block.get("type") == "output_text":
                        text_parts.append(block.get("text", ""))

            elif item_type == "function_call":
                tool_calls.append({
                    "id": item.get("call_id", ""),
                    "type": "function",
                    "function": {
                        "name": item.get("name", ""),
                        "arguments": item.get("arguments", "{}"),
                    },
                })

        finish_reason = "tool_calls" if tool_calls else "stop"
        message: dict[str, Any] = {
            "role": "assistant",
            "content": "\n".join(text_parts) or None,
        }
        if tool_calls:
            message["tool_calls"] = tool_calls

        return {
            "choices": [
                {"index": 0, "message": message, "finish_reason": finish_reason}
            ],
            "model": raw.get("model", ""),
        }

    # ── Main call ────────────────────────────────────────────────────────────

    async def chat(
        self,
        messages: list[dict[str, Any]],
        model: str,
        temperature: float,
        tools: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        log.debug(
            "OpenAICompatibleClient.chat: auth_mode=%s base=%s model=%s",
            self._auth_mode, self._base, model,
        )
        if self._auth_mode == "codex_oauth":
            return await self._chat_responses(messages, model, temperature, tools)
        return await self._chat_completions(messages, model, temperature, tools)

    async def _chat_completions(
        self,
        messages: list[dict[str, Any]],
        model: str,
        temperature: float,
        tools: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        """Standard Chat Completions API call."""
        url = f"{self._base}/chat/completions"
        log.debug("_chat_completions: POST %s", url)
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(url, json=payload, headers=self._headers())
            log.debug("_chat_completions: status=%s", r.status_code)
            r.raise_for_status()
            return r.json()

    async def _chat_responses(
        self,
        messages: list[dict[str, Any]],
        model: str,
        temperature: float,
        tools: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        """Responses API call (used for Codex OAuth tokens).

        ChatGPT OAuth tokens must go to chatgpt.com/backend-api/codex,
        NOT to api.openai.com.  If the user set a custom LLM_BASE_URL
        we respect that; otherwise we override to the ChatGPT backend.
        """
        # Use ChatGPT backend unless user explicitly set a base URL
        if self._base == "https://api.openai.com/v1":
            base = _CODEX_CHATGPT_BASE
        else:
            base = self._base
        url = f"{base}/responses"
        log.info("_chat_responses: POST %s (codex_oauth)", url)
        instructions, input_items = self._messages_to_responses(messages)

        payload: dict[str, Any] = {
            "model": model,
            "input": input_items,
            "store": False,
            "stream": True,
        }
        if instructions:
            payload["instructions"] = instructions
        if tools:
            payload["tools"] = self._tools_to_responses(tools)
            payload["tool_choice"] = "auto"

        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream(
                "POST", url, json=payload, headers=self._headers(),
            ) as r:
                log.debug("_chat_responses: status=%s", r.status_code)
                if r.status_code >= 400:
                    body = await r.aread()
                    log.warning("_chat_responses: error body=%s", body[:500])
                    r.raise_for_status()
                return self._responses_to_openai(
                    await self._consume_sse(r)
                )

    @staticmethod
    async def _consume_sse(response: httpx.Response) -> dict:
        """Read an SSE stream and return the completed Responses API object.

        The ChatGPT backend requires ``stream: true``.  We accumulate
        the full response from the ``response.completed`` event which
        carries the entire response object including all output items.
        """
        completed: dict = {}
        event_type = ""

        async for raw_line in response.aiter_lines():
            line = raw_line.strip()
            if not line:
                # Empty line = event boundary; reset for next event
                event_type = ""
                continue
            if line.startswith("event:"):
                event_type = line[len("event:"):].strip()
                continue
            if line.startswith("data:"):
                data_str = line[len("data:"):].strip()
                if data_str == "[DONE]":
                    break
                if event_type == "response.completed":
                    try:
                        completed = json.loads(data_str)
                    except json.JSONDecodeError:
                        log.warning("_consume_sse: bad JSON in response.completed")
                continue

        if not completed:
            log.warning("_consume_sse: no response.completed event received")
        return completed


# ── Anthropic ─────────────────────────────────────────────────────────────────

_ANTHROPIC_API = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"
_ANTHROPIC_MAX_TOKENS = 4096


class AnthropicClient(BaseLLMClient):
    """Adapter for the Anthropic Messages API.

    Wire format differences vs OpenAI:
    ┌─────────────────────────────────┬───────────────────────────────────────┐
    │ OpenAI                          │ Anthropic                             │
    ├─────────────────────────────────┼───────────────────────────────────────┤
    │ POST /v1/chat/completions       │ POST /v1/messages                     │
    │ Authorization: Bearer <key>     │ x-api-key: <key>                      │
    │ messages[].role = "system"      │ top-level "system" string             │
    │ messages[].role = "tool"        │ user message with tool_result blocks  │
    │ assistant.tool_calls[].function │ assistant content tool_use blocks     │
    │ tools[].function.parameters     │ tools[].input_schema                  │
    │ finish_reason = "tool_calls"    │ stop_reason = "tool_use"              │
    │ temperature 0–2                 │ temperature 0–1 (clamped)             │
    └─────────────────────────────────┴───────────────────────────────────────┘
    """

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    # ── Format converters ─────────────────────────────────────────────────────

    @staticmethod
    def _tools_to_anthropic(tools: list[dict]) -> list[dict]:
        """OpenAI tools → Anthropic tools (rename parameters → input_schema)."""
        result = []
        for t in tools:
            fn = t.get("function", {})
            result.append(
                {
                    "name": fn.get("name", ""),
                    "description": fn.get("description", ""),
                    "input_schema": fn.get(
                        "parameters", {"type": "object", "properties": {}}
                    ),
                }
            )
        return result

    @staticmethod
    def _messages_to_anthropic(
        messages: list[dict],
    ) -> tuple[str, list[dict]]:
        """Split OpenAI-format messages into (system_string, anthropic_messages).

        Rules:
        - "system" roles are concatenated into the top-level system string.
        - "user" roles are converted directly.
        - "assistant" messages with tool_calls become assistant content blocks.
        - "tool" roles become user messages with tool_result content blocks.
          Consecutive tool results are merged into a single user message.
        """
        system_parts: list[str] = []
        converted: list[dict] = []

        for msg in messages:
            role = msg.get("role", "")

            if role == "system":
                if msg.get("content"):
                    system_parts.append(msg["content"])
                continue

            if role == "user":
                converted.append(
                    {"role": "user", "content": msg.get("content", "")}
                )
                continue

            if role == "assistant":
                tool_calls: list[dict] = msg.get("tool_calls") or []
                text: str = msg.get("content") or ""
                if tool_calls:
                    blocks: list[dict] = []
                    if text:
                        blocks.append({"type": "text", "text": text})
                    for tc in tool_calls:
                        fn = tc.get("function", {})
                        try:
                            inp = json.loads(fn.get("arguments", "{}"))
                        except json.JSONDecodeError:
                            inp = {}
                        blocks.append(
                            {
                                "type": "tool_use",
                                "id": tc.get("id", f"toolu_{hashlib.md5(fn.get('name','').encode()).hexdigest()[:12]}"),
                                "name": fn.get("name", ""),
                                "input": inp,
                            }
                        )
                    converted.append({"role": "assistant", "content": blocks})
                else:
                    converted.append(
                        {"role": "assistant", "content": text or ""}
                    )
                continue

            if role == "tool":
                result_block = {
                    "type": "tool_result",
                    "tool_use_id": msg.get("tool_call_id", ""),
                    "content": msg.get("content", ""),
                }
                # Merge consecutive tool results into one user message.
                if converted and converted[-1]["role"] == "user" and isinstance(
                    converted[-1]["content"], list
                ):
                    converted[-1]["content"].append(result_block)
                else:
                    converted.append({"role": "user", "content": [result_block]})
                continue

        return "\n\n".join(system_parts), converted

    @staticmethod
    def _response_to_openai(raw: dict) -> dict:
        """Convert an Anthropic Messages response to OpenAI-compatible format."""
        content_blocks: list[dict] = raw.get("content", [])
        stop_reason: str = raw.get("stop_reason", "end_turn")

        text_parts: list[str] = []
        tool_calls: list[dict] = []

        for block in content_blocks:
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                tool_calls.append(
                    {
                        "id": block.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": block.get("name", ""),
                            "arguments": json.dumps(block.get("input", {})),
                        },
                    }
                )

        finish_reason = "stop" if stop_reason == "end_turn" else "tool_calls"
        message: dict[str, Any] = {
            "role": "assistant",
            "content": "\n".join(text_parts) or None,
        }
        if tool_calls:
            message["tool_calls"] = tool_calls

        return {
            "choices": [
                {"index": 0, "message": message, "finish_reason": finish_reason}
            ],
            "model": raw.get("model", ""),
        }

    # ── Main call ─────────────────────────────────────────────────────────────

    async def chat(
        self,
        messages: list[dict[str, Any]],
        model: str,
        temperature: float,
        tools: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        system, ant_messages = self._messages_to_anthropic(messages)
        # Anthropic temperature range is 0–1; clamp if caller passes OpenAI range.
        temp = max(0.0, min(1.0, temperature))

        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": _ANTHROPIC_MAX_TOKENS,
            "messages": ant_messages,
            "temperature": temp,
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = self._tools_to_anthropic(tools)

        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(_ANTHROPIC_API, json=payload, headers=headers)
            r.raise_for_status()
            return self._response_to_openai(r.json())


# ── Store fallback (sync, no refresh) ─────────────────────────────────────────

def _resolve_from_store(provider: str) -> tuple[str, str, str]:
    """Read credentials directly from the auth store (sync, no token refresh).

    Returns (api_key, oauth_token, account_id).
    Falls back to empty strings on any error.
    Cannot call asyncio.run() — already running inside Uvicorn's event loop.
    """
    try:
        from auth.store import get_store
        store = get_store()
        data = store.load()
        import time
        now_ms = int(time.time() * 1000)

        candidates = []
        last_good = data.lastGood.get(provider)
        if last_good and last_good in data.profiles:
            candidates.append(last_good)
        for pid in data.order.get(provider, []):
            if pid in data.profiles and pid not in candidates:
                candidates.append(pid)

        log.debug(
            "_resolve_from_store: provider=%s candidates=%s lastGood=%s",
            provider, candidates, last_good,
        )

        for pid in candidates:
            raw = data.profiles[pid]
            ctype = raw.get("type")
            log.debug("_resolve_from_store: checking pid=%s type=%s", pid, ctype)
            if ctype == "api_key":
                log.debug("_resolve_from_store: → api_key found")
                return raw.get("key", ""), "", ""
            elif ctype == "token":
                expires = raw.get("expires")
                if expires and expires < now_ms:
                    log.debug("_resolve_from_store: token %s expired", pid)
                    continue
                log.debug("_resolve_from_store: → token found")
                return "", raw.get("token", ""), ""
            elif ctype == "oauth":
                expires = raw.get("expires", 0)
                if expires < now_ms:
                    log.debug("_resolve_from_store: oauth %s expired", pid)
                    continue  # Expired; skip (no refresh in sync path)
                account_id = raw.get("accountId", "")
                log.debug("_resolve_from_store: → oauth token found")
                return "", raw.get("access", ""), account_id
            else:
                log.debug("_resolve_from_store: unknown type %s for pid=%s", ctype, pid)

        log.debug("_resolve_from_store: no usable credential for %s", provider)
    except Exception as exc:
        log.warning("_resolve_from_store: exception: %s", exc)
    return "", "", ""


# ── Factory ───────────────────────────────────────────────────────────────────

def make_llm_client() -> BaseLLMClient:
    """Instantiate the correct LLM client.

    Credential resolution order:
      1. Auth Store (managed via the Auth tab in the UI)
         - OAuth credential found  → codex_oauth mode
         - API key credential found → api_key mode
      2. LLM_API_KEY env var (classic config fallback)

    LLM_OAUTH_TOKEN and OPENAI_AUTH_MODE are no longer read from the
    environment; use the Auth tab to manage OAuth tokens.
    """
    provider = os.environ.get("LLM_PROVIDER", "openai_compatible")
    base_url = os.environ.get("LLM_BASE_URL", "")
    env_api_key = os.environ.get("LLM_API_KEY", "")

    log.debug(
        "make_llm_client: provider=%s base_url_set=%s env_api_key_set=%s",
        provider, bool(base_url), bool(env_api_key),
    )

    if provider == "anthropic":
        # Try store first, fall back to env var.
        api_key, _, _ = _resolve_from_store("anthropic")
        if not api_key:
            api_key = env_api_key
        log.info("make_llm_client: → AnthropicClient")
        return AnthropicClient(api_key=api_key)

    # openai_compatible | ollama | custom_http
    # Check store for an OAuth token (codex flow) first.
    store_key, store_oauth, account_id = _resolve_from_store("openai-codex")
    log.debug(
        "make_llm_client: store lookup → key_set=%s oauth_set=%s account_id_set=%s",
        bool(store_key), bool(store_oauth), bool(account_id),
    )
    if store_oauth:
        log.info("make_llm_client: → OpenAICompatibleClient (codex_oauth → chatgpt.com)")
        return OpenAICompatibleClient(
            provider=provider,
            base_url=base_url,
            api_key=store_key or env_api_key,
            auth_mode="codex_oauth",
            oauth_token=store_oauth,
            account_id=account_id,
        )

    # No OAuth in store — use API key (store or env var).
    api_key = store_key or env_api_key
    log.info("make_llm_client: → OpenAICompatibleClient (api_key → /chat/completions)")
    return OpenAICompatibleClient(
        provider=provider,
        base_url=base_url,
        api_key=api_key,
        auth_mode="api_key",
        oauth_token="",
    )
