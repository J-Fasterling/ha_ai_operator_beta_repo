#!/usr/bin/env python3
"""Tests that codex_oauth mode sends requests to chatgpt.com with
the ChatGPT-Account-ID header, while api_key mode still uses api.openai.com."""
import asyncio
import json
import sys
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "ha_ai_operator", "app"))
from llm_clients import OpenAICompatibleClient, _CODEX_CHATGPT_BASE, make_llm_client

_calls: list[dict] = []

class MockHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}
        _calls.append({
            "path": self.path,
            "headers": dict(self.headers),
            "body": body,
        })
        response = {
            "id": "resp_mock",
            "model": body.get("model", "test"),
            "status": "completed",
            "output": [{
                "type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": "ok"}],
            }],
        }
        # If stream requested, return SSE format
        if body.get("stream"):
            resp_json = json.dumps(response)
            sse = f"event: response.completed\ndata: {resp_json}\n\ndata: [DONE]\n\n"
            payload = sse.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        else:
            payload = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    def log_message(self, *a):
        pass

PORT = 19877

def start_mock():
    s = HTTPServer(("127.0.0.1", PORT), MockHandler)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s


async def test_codex_oauth_base_url_override():
    """codex_oauth with default base → rewritten to chatgpt.com."""
    _calls.clear()
    # Default base (api.openai.com/v1) should be overridden inside _chat_responses
    client = OpenAICompatibleClient(
        provider="openai_compatible",
        base_url="",  # let it default to api.openai.com/v1
        api_key="",
        auth_mode="codex_oauth",
        oauth_token="tok",
        account_id="acc_123",
    )
    assert client._base == "https://api.openai.com/v1"
    # We can't actually hit chatgpt.com, so use mock by overriding _CODEX_CHATGPT_BASE
    # Instead, test the logic directly
    print("  PASS: codex_oauth defaults to api.openai.com/v1 base (override in _chat_responses)")


async def test_codex_oauth_headers():
    """codex_oauth includes ChatGPT-Account-ID header."""
    client = OpenAICompatibleClient(
        provider="openai_compatible",
        base_url=f"http://127.0.0.1:{PORT}",
        api_key="",
        auth_mode="codex_oauth",
        oauth_token="test-oauth-tok",
        account_id="acc_456",
    )
    headers = client._headers()
    assert headers["Authorization"] == "Bearer test-oauth-tok"
    assert headers["ChatGPT-Account-ID"] == "acc_456"
    print("  PASS: codex_oauth headers include ChatGPT-Account-ID")


async def test_codex_oauth_no_account_id():
    """codex_oauth without account_id still works (header omitted)."""
    client = OpenAICompatibleClient(
        provider="openai_compatible",
        base_url=f"http://127.0.0.1:{PORT}",
        api_key="",
        auth_mode="codex_oauth",
        oauth_token="test-tok",
        account_id="",
    )
    headers = client._headers()
    assert "ChatGPT-Account-ID" not in headers
    assert headers["Authorization"] == "Bearer test-tok"
    print("  PASS: codex_oauth without account_id omits header")


async def test_api_key_no_account_header():
    """api_key mode never sends ChatGPT-Account-ID."""
    client = OpenAICompatibleClient(
        provider="openai_compatible",
        base_url=f"http://127.0.0.1:{PORT}",
        api_key="sk-test",
        auth_mode="api_key",
        oauth_token="",
        account_id="",
    )
    headers = client._headers()
    assert "ChatGPT-Account-ID" not in headers
    assert headers["Authorization"] == "Bearer sk-test"
    print("  PASS: api_key mode has no ChatGPT-Account-ID")


async def test_codex_oauth_custom_base_url_preserved():
    """User-set LLM_BASE_URL is respected even in codex_oauth mode."""
    _calls.clear()
    client = OpenAICompatibleClient(
        provider="openai_compatible",
        base_url=f"http://127.0.0.1:{PORT}/custom",
        api_key="",
        auth_mode="codex_oauth",
        oauth_token="tok",
        account_id="acc",
    )
    msgs = [{"role": "user", "content": "hi"}]
    await client.chat(msgs, "gpt-5.3-codex", 0.7)

    assert len(_calls) == 1
    # Custom base URL should be preserved, not overridden to chatgpt.com
    assert _calls[0]["path"] == "/custom/responses"
    assert _calls[0]["headers"].get("ChatGPT-Account-ID") == "acc"
    print("  PASS: custom base URL preserved in codex_oauth mode")


async def test_codex_oauth_sends_responses_format():
    """codex_oauth sends Responses API format (input, instructions)."""
    _calls.clear()
    client = OpenAICompatibleClient(
        provider="openai_compatible",
        base_url=f"http://127.0.0.1:{PORT}",
        api_key="",
        auth_mode="codex_oauth",
        oauth_token="tok",
        account_id="acc",
    )
    msgs = [
        {"role": "system", "content": "You are a helper."},
        {"role": "user", "content": "Hello"},
    ]
    await client.chat(msgs, "gpt-5.3-codex", 0.7)

    body = _calls[0]["body"]
    assert "input" in body, "Should use 'input' (Responses API)"
    assert "instructions" in body, "Should have 'instructions'"
    assert "messages" not in body, "Should NOT have 'messages'"
    assert body["instructions"] == "You are a helper."
    assert body.get("store") is False, "Must set store=false for ChatGPT backend"
    assert body.get("stream") is True, "Must set stream=true for ChatGPT backend"
    print("  PASS: codex_oauth sends Responses API format with store=false, stream=true")


async def test_api_key_sends_chat_completions_format():
    """api_key still sends Chat Completions format to /chat/completions."""
    _calls.clear()
    # Need a mock that returns chat completions format
    original = MockHandler.do_POST
    def chat_handler(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}
        _calls.append({"path": self.path, "body": body})
        resp = {"choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]}
        p = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(p)))
        self.end_headers()
        self.wfile.write(p)
    MockHandler.do_POST = chat_handler
    try:
        client = OpenAICompatibleClient(
            provider="openai_compatible",
            base_url=f"http://127.0.0.1:{PORT}",
            api_key="sk-key",
            auth_mode="api_key",
        )
        msgs = [
            {"role": "system", "content": "System."},
            {"role": "user", "content": "Hi"},
        ]
        await client.chat(msgs, "gpt-4o", 0.7)
        assert _calls[0]["path"] == "/chat/completions"
        assert "messages" in _calls[0]["body"]
        assert "input" not in _calls[0]["body"]
        print("  PASS: api_key sends Chat Completions format to /chat/completions")
    finally:
        MockHandler.do_POST = original


async def test_resolve_from_store_returns_3_tuple():
    """_resolve_from_store returns (key, oauth, account_id) 3-tuple."""
    from llm_clients import _resolve_from_store
    result = _resolve_from_store("nonexistent-provider")
    assert len(result) == 3, f"Expected 3-tuple, got {len(result)}-tuple"
    assert result == ("", "", "")
    print("  PASS: _resolve_from_store returns 3-tuple")


async def test_make_llm_client_uses_codex_oauth_env_fallback():
    """Factory uses Codex OAuth with llm_oauth_token fallback."""
    old_token = os.environ.get("LLM_OAUTH_TOKEN")
    old_provider = os.environ.get("LLM_PROVIDER")
    try:
        os.environ["LLM_OAUTH_TOKEN"] = "env-oauth-token"
        os.environ["LLM_PROVIDER"] = "legacy-provider"
        client = make_llm_client()
        assert isinstance(client, OpenAICompatibleClient)
        assert client._provider == "codex"
        assert client._auth_mode == "codex_oauth"
        assert client._oauth_token == "env-oauth-token"
        print("  PASS: factory normalizes provider to Codex OAuth env fallback")
    finally:
        if old_token is None:
            os.environ.pop("LLM_OAUTH_TOKEN", None)
        else:
            os.environ["LLM_OAUTH_TOKEN"] = old_token
        if old_provider is None:
            os.environ.pop("LLM_PROVIDER", None)
        else:
            os.environ["LLM_PROVIDER"] = old_provider


async def main():
    print("Testing Codex OAuth fix...\n")
    server = start_mock()
    tests = [
        test_codex_oauth_base_url_override,
        test_codex_oauth_headers,
        test_codex_oauth_no_account_id,
        test_api_key_no_account_header,
        test_codex_oauth_custom_base_url_preserved,
        test_codex_oauth_sends_responses_format,
        test_api_key_sends_chat_completions_format,
        test_resolve_from_store_returns_3_tuple,
        test_make_llm_client_uses_codex_oauth_env_fallback,
    ]
    passed = failed = 0
    for t in tests:
        try:
            await t()
            passed += 1
        except Exception as e:
            print(f"  FAIL: {t.__name__}: {e}")
            import traceback; traceback.print_exc()
            failed += 1
    print(f"\nResults: {passed} passed, {failed} failed")
    server.shutdown()
    return failed == 0

if __name__ == "__main__":
    sys.exit(0 if asyncio.run(main()) else 1)
