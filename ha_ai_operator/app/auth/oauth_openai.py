"""OpenAI Codex OAuth (PKCE) helpers.

Flow:
1. generate_pkce() → (verifier, challenge)
2. build_authorize_url(state, challenge) → show to user
3. User logs in, browser redirects to localhost:1455/auth/callback?code=…&state=…
4. exchange_code(code, verifier) → OAuthCredential
5. On expiry: refresh_token(cred) → OAuthCredential
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import secrets
import time
import urllib.parse
from collections import OrderedDict
from typing import Optional, Tuple

import httpx

from auth.store import OAuthCredential

log = logging.getLogger("ha_ai_operator.auth.oauth_openai")

# ── Constants ──────────────────────────────────────────────────────────────────

_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
_AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
_TOKEN_URL = "https://auth.openai.com/oauth/token"
_REDIRECT_URI = "http://localhost:1455/auth/callback"
_SCOPE = "openid profile email offline_access"

# ── PKCE ──────────────────────────────────────────────────────────────────────

def generate_pkce() -> Tuple[str, str]:
    """Return (verifier, challenge) for PKCE S256."""
    verifier_bytes = secrets.token_bytes(32)
    verifier = base64.urlsafe_b64encode(verifier_bytes).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def generate_state() -> str:
    return secrets.token_hex(16)


def build_authorize_url(state: str, challenge: str) -> str:
    params = {
        "client_id": _CLIENT_ID,
        "redirect_uri": _REDIRECT_URI,
        "response_type": "code",
        "scope": _SCOPE,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "originator": "ha-ai-operator",
    }
    return f"{_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


# ── PKCE session store (module-level, max 10 entries, FIFO) ───────────────────

_pkce_sessions: OrderedDict[str, dict] = OrderedDict()
_MAX_SESSIONS = 10


def store_pkce_session(state: str, verifier: str, expires_in: int = 600) -> None:
    _pkce_sessions[state] = {
        "verifier": verifier,
        "created": int(time.time()),
        "expires": int(time.time()) + expires_in,
    }
    while len(_pkce_sessions) > _MAX_SESSIONS:
        _pkce_sessions.popitem(last=False)


def pop_pkce_session(state: Optional[str]) -> Optional[str]:
    """Return verifier for state (and remove), or None if not found/expired."""
    if state is None:
        # Best-effort: return oldest session's verifier
        if _pkce_sessions:
            _, session = next(iter(_pkce_sessions.items()))
            if session["expires"] > int(time.time()):
                first_state = next(iter(_pkce_sessions))
                _pkce_sessions.pop(first_state, None)
                return session["verifier"]
        return None
    session = _pkce_sessions.pop(state, None)
    if session is None:
        return None
    if session["expires"] < int(time.time()):
        return None
    return session["verifier"]


# ── Token exchange & refresh ───────────────────────────────────────────────────

async def exchange_code(code: str, verifier: str) -> OAuthCredential:
    """Exchange authorization code for tokens; returns OAuthCredential."""
    payload = {
        "client_id": _CLIENT_ID,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": _REDIRECT_URI,
        "code_verifier": verifier,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            _TOKEN_URL,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        r.raise_for_status()
        tokens = r.json()

    access = tokens["access_token"]
    refresh = tokens.get("refresh_token", "")
    expires_in = tokens.get("expires_in", 3600)
    expires_ms = (int(time.time()) + int(expires_in)) * 1000

    account_id = decode_account_id(access)
    log.info("oauth: code exchange successful account_id=%s", account_id or "unknown")

    return OAuthCredential(
        access=access,
        refresh=refresh,
        expires=expires_ms,
        accountId=account_id,
        clientId=_CLIENT_ID,
    )


async def refresh_token(cred: OAuthCredential) -> OAuthCredential:
    """Refresh OAuth tokens using the refresh token."""
    payload = {
        "client_id": _CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": cred.refresh,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            _TOKEN_URL,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        r.raise_for_status()
        tokens = r.json()

    access = tokens["access_token"]
    new_refresh = tokens.get("refresh_token", cred.refresh)
    expires_in = tokens.get("expires_in", 3600)
    expires_ms = (int(time.time()) + int(expires_in)) * 1000

    log.info("oauth: token refresh successful")
    return OAuthCredential(
        access=access,
        refresh=new_refresh,
        expires=expires_ms,
        accountId=cred.accountId,
        clientId=cred.clientId,
        email=cred.email,
    )


def parse_authorization_input(raw: str) -> Tuple[str, Optional[str]]:
    """Accept full redirect URL, code#state, or bare code.

    Returns (code, state_or_None).
    """
    raw = raw.strip()
    # Full URL?
    if raw.startswith("http"):
        parsed = urllib.parse.urlparse(raw)
        qs = urllib.parse.parse_qs(parsed.query)
        code = (qs.get("code") or [""])[0]
        state = (qs.get("state") or [None])[0]
        return code, state
    # code#state format?
    if "#" in raw:
        parts = raw.split("#", 1)
        return parts[0].strip(), parts[1].strip()
    # Bare code
    return raw, None


def decode_account_id(access_token: str) -> Optional[str]:
    """Decode JWT payload (no signature verify) to extract chatgpt_account_id."""
    try:
        import jwt  # PyJWT
        payload = jwt.decode(
            access_token,
            options={"verify_signature": False},
            algorithms=["RS256", "HS256"],
        )
        auth_claim = payload.get("https://api.openai.com/auth", {})
        if isinstance(auth_claim, dict):
            return auth_claim.get("chatgpt_account_id")
    except Exception as exc:
        log.debug("oauth: jwt decode failed: %s", exc)
    return None


# ── Callback server (best-effort, localhost only) ─────────────────────────────

_cb_server: Optional[asyncio.AbstractServer] = None
_cb_results: dict[str, Tuple[str, Optional[str]]] = {}
_cb_event: Optional[asyncio.Event] = None


async def start_callback_server(expected_state: str) -> bool:
    """Start a local HTTP server on 127.0.0.1:1455 to capture OAuth callback.

    Returns True if server started successfully.
    In HA containers this only works if the browser is on the same host.
    Always show manual paste fallback prominently.
    """
    global _cb_server, _cb_results, _cb_event

    await _stop_callback_server()

    _cb_results.clear()
    _cb_event = asyncio.Event()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            data = await asyncio.wait_for(reader.read(4096), timeout=5.0)
            request_line = data.decode(errors="replace").split("\r\n")[0]
            # GET /auth/callback?code=…&state=… HTTP/1.1
            parts = request_line.split(" ")
            if len(parts) >= 2:
                path = parts[1]
                parsed = urllib.parse.urlparse(path)
                qs = urllib.parse.parse_qs(parsed.query)
                code = (qs.get("code") or [""])[0]
                state = (qs.get("state") or [None])[0]
                if code:
                    _cb_results["latest"] = (code, state)
                    if _cb_event:
                        _cb_event.set()
                    log.info("oauth callback: received code state=%s", state)
            # Send minimal HTML response
            body = b"<html><body><h2>Authorization received. You may close this tab.</h2></body></html>"
            response = (
                b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
            )
            writer.write(response)
            await writer.drain()
        except Exception as exc:
            log.debug("oauth callback handler error: %s", exc)
        finally:
            writer.close()

    try:
        _cb_server = await asyncio.start_server(handle, "127.0.0.1", 1455)
        log.info("oauth: callback server started on 127.0.0.1:1455")
        return True
    except OSError as exc:
        log.warning("oauth: callback server could not start (port busy?): %s", exc)
        _cb_server = None
        return False


def get_callback_result() -> Optional[Tuple[str, Optional[str]]]:
    """Return (code, state) if callback received, else None."""
    return _cb_results.get("latest")


async def _stop_callback_server() -> None:
    global _cb_server
    if _cb_server is not None:
        _cb_server.close()
        try:
            await _cb_server.wait_closed()
        except Exception:
            pass
        _cb_server = None
