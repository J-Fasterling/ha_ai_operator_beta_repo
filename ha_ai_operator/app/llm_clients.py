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

Supported backend
─────────────────
  codex             ChatGPT Codex backend via OAuth, exposed internally as an
                    OpenAI-compatible chat client for the agent.

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


# ── OpenAI-compatible wire adapter for Codex ──────────────────────────────────

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
            if not (self._oauth_token or self._api_key):
                raise RuntimeError(
                    "Codex OAuth is not configured. Start the OpenAI Codex login "
                    "in the Auth tab or set llm_oauth_token in the add-on config."
                )
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

        The ChatGPT backend requires ``stream: true``.  We try to capture
        the full response from the ``response.completed`` event.  As a
        fallback we accumulate output items from ``response.output_item.done``
        events and text deltas from ``response.output_text.delta`` events,
        then build the response ourselves.
        """
        completed: dict = {}
        created: dict = {}
        done_items: list[dict] = []
        text_deltas: list[str] = []
        text_done = ""
        event_type = ""
        seen_events: set[str] = set()

        async for raw_line in response.aiter_lines():
            line = raw_line.strip()
            if not line:
                event_type = ""
                continue
            if line.startswith("event:"):
                event_type = line[len("event:"):].strip()
                seen_events.add(event_type)
                continue
            if not line.startswith("data:"):
                continue
            data_str = line[len("data:"):].strip()
            if data_str == "[DONE]":
                break
            try:
                payload = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            actual_event_type = event_type or payload.get("type", "")

            if actual_event_type == "response.completed":
                response_obj = payload.get("response") if isinstance(payload, dict) else None
                completed = response_obj if isinstance(response_obj, dict) else payload
            elif actual_event_type == "response.created":
                response_obj = payload.get("response") if isinstance(payload, dict) else None
                created = response_obj if isinstance(response_obj, dict) else payload
            elif actual_event_type == "response.output_item.done":
                item = payload.get("item") if isinstance(payload, dict) else None
                done_items.append(item if isinstance(item, dict) else payload)
            elif actual_event_type == "response.output_text.delta":
                text_deltas.append(payload.get("delta", ""))
            elif actual_event_type == "response.output_text.done":
                text_done = payload.get("text", "") or text_done

        log.debug("_consume_sse: events_seen=%s", sorted(seen_events))

        if completed and completed.get("output"):
            return completed
        if completed:
            log.debug("_consume_sse: response.completed had no output; using fallback events")

        # Fallback: build response from accumulated items / deltas
        if done_items:
            log.debug(
                "_consume_sse: no response.completed, building from %d done_items",
                len(done_items),
            )
            return {
                "id": created.get("id", ""),
                "model": created.get("model", ""),
                "status": "completed",
                "output": done_items,
            }

        text = text_done or "".join(text_deltas)
        if text:
            log.debug(
                "_consume_sse: no done_items, building from text length=%d",
                len(text),
            )
            return {
                "id": created.get("id", ""),
                "model": created.get("model", ""),
                "status": "completed",
                "output": [{
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text}],
                }],
            }

        log.warning("_consume_sse: no usable events received (saw: %s)", sorted(seen_events))
        return {}


# ── Store fallback ────────────────────────────────────────────────────────────

def _refresh_oauth_profile_sync(profile_id: str, raw: dict[str, Any]) -> tuple[str, str]:
    """Refresh an expired Codex OAuth profile from sync code.

    The agent constructs its LLM client synchronously per request, so it cannot
    use the async resolver without changing the call graph. Keeping a small sync
    refresh here avoids forcing users to re-login whenever the access token has
    expired but the refresh token is still valid.
    """
    refresh = raw.get("refresh", "")
    if not refresh:
        return "", ""

    from auth.oauth_openai import _CLIENT_ID, _TOKEN_URL, decode_account_id
    from auth.store import get_store
    import time

    payload = {
        "client_id": _CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": refresh,
    }
    with httpx.Client(timeout=30.0) as client:
        response = client.post(
            _TOKEN_URL,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response.raise_for_status()
        tokens = response.json()

    access = tokens["access_token"]
    new_refresh = tokens.get("refresh_token", refresh)
    expires_in = tokens.get("expires_in", 3600)
    expires_ms = (int(time.time()) + int(expires_in)) * 1000
    account_id = decode_account_id(access) or raw.get("accountId", "") or ""
    get_store().update_oauth_tokens(profile_id, access, new_refresh, expires_ms)
    log.info("_resolve_from_store: refreshed expired Codex OAuth profile %s", profile_id)
    return access, account_id

def _resolve_from_store(provider: str) -> tuple[str, str, str]:
    """Read credentials directly from the auth store.

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
                    try:
                        access, account_id = _refresh_oauth_profile_sync(pid, raw)
                    except Exception as exc:
                        log.warning(
                            "_resolve_from_store: oauth refresh failed for %s: %s",
                            pid, exc,
                        )
                        continue
                    if access:
                        return "", access, account_id
                    continue
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
    """Instantiate the Codex LLM client.

    Credential resolution order:
      1. Auth Store (managed via the Auth tab in the UI)
      2. LLM_OAUTH_TOKEN env var (classic config fallback)
    """
    provider = "codex"
    base_url = os.environ.get("LLM_BASE_URL", "")
    env_oauth_token = os.environ.get("LLM_OAUTH_TOKEN", "")

    log.debug(
        "make_llm_client: provider=%s base_url_set=%s env_oauth_token_set=%s",
        provider, bool(base_url), bool(env_oauth_token),
    )

    store_key, store_oauth, account_id = _resolve_from_store("openai-codex")
    log.debug(
        "make_llm_client: store lookup → key_set=%s oauth_set=%s account_id_set=%s",
        bool(store_key), bool(store_oauth), bool(account_id),
    )

    oauth_token = store_oauth or env_oauth_token
    log.info("make_llm_client: → OpenAICompatibleClient (codex_oauth → chatgpt.com)")
    return OpenAICompatibleClient(
        provider=provider,
        base_url=base_url,
        api_key=store_key,
        auth_mode="codex_oauth",
        oauth_token=oauth_token,
        account_id=account_id,
    )
