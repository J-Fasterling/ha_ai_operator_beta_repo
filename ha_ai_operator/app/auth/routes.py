"""FastAPI router for all /auth/* and /oauth/* endpoints."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from auth.store import get_store, OAuthCredential

log = logging.getLogger("ha_ai_operator.auth.routes")

router = APIRouter()


# ── Request / Response models ──────────────────────────────────────────────────

class AnthropicKeyRequest(BaseModel):
    key: str


class AnthropicTokenRequest(BaseModel):
    token: str
    expires_ms: Optional[int] = None  # ms epoch; None = no expiry


class OAuthCompleteRequest(BaseModel):
    input: str   # Full URL, code#state, or bare code
    state: Optional[str] = None


class ProfileSummary(BaseModel):
    profileId: str
    type: str
    provider: str
    expiresIso: Optional[str] = None
    isExpired: bool = False
    errorCount: int = 0


def _profile_summary(pid: str, raw: dict, stats_map: dict) -> ProfileSummary:
    """Build a ProfileSummary — never include key/token/access/refresh."""
    cred_type = raw.get("type", "unknown")
    provider = raw.get("provider", "unknown")
    expires_ms: Optional[int] = raw.get("expires")
    expires_iso: Optional[str] = None
    is_expired = False
    if expires_ms:
        expires_iso = datetime.fromtimestamp(
            expires_ms / 1000, tz=timezone.utc
        ).isoformat()
        is_expired = expires_ms < int(time.time() * 1000)
    stats = stats_map.get(pid)
    error_count = stats.errorCount if stats else 0
    return ProfileSummary(
        profileId=pid,
        type=cred_type,
        provider=provider,
        expiresIso=expires_iso,
        isExpired=is_expired,
        errorCount=error_count,
    )


# ── Anthropic ──────────────────────────────────────────────────────────────────

@router.post("/auth/anthropic/api-key")
async def add_anthropic_key(req: AnthropicKeyRequest) -> ProfileSummary:
    from auth.anthropic_auth import add_api_key
    store = get_store()
    try:
        pid = add_api_key(req.key, store)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    data = store.load()
    return _profile_summary(pid, data.profiles[pid], data.usageStats)


@router.post("/auth/anthropic/setup-token")
async def add_anthropic_token(req: AnthropicTokenRequest) -> ProfileSummary:
    from auth.anthropic_auth import add_setup_token
    store = get_store()
    try:
        pid = add_setup_token(req.token, req.expires_ms, store)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    data = store.load()
    return _profile_summary(pid, data.profiles[pid], data.usageStats)


# ── OpenAI Codex OAuth ─────────────────────────────────────────────────────────

@router.post("/oauth/openai-codex/start")
async def oauth_start() -> Dict[str, Any]:
    from auth.oauth_openai import (
        generate_pkce,
        generate_state,
        build_authorize_url,
        store_pkce_session,
        start_callback_server,
    )
    verifier, challenge = generate_pkce()
    state = generate_state()
    store_pkce_session(state, verifier)
    auth_url = build_authorize_url(state, challenge)
    cb_active = await start_callback_server(state)
    log.info("oauth: started flow state=%s callback_server=%s", state, cb_active)
    return {
        "auth_url": auth_url,
        "state": state,
        "callback_server_active": cb_active,
        "redirect_uri": "http://localhost:1455/auth/callback",
        "note": (
            "Open auth_url in your browser. "
            "If the callback server is not active, paste the full redirect URL or code below."
        ),
    }


@router.get("/oauth/openai-codex/status")
async def oauth_status() -> Dict[str, Any]:
    from auth.oauth_openai import get_callback_result
    result = get_callback_result()
    return {"received": result is not None, "has_code": result is not None}


@router.post("/oauth/openai-codex/complete")
async def oauth_complete(req: OAuthCompleteRequest) -> ProfileSummary:
    from auth.oauth_openai import (
        parse_authorization_input,
        pop_pkce_session,
        exchange_code,
    )
    store = get_store()

    code, detected_state = parse_authorization_input(req.input)
    if not code:
        raise HTTPException(status_code=400, detail="No authorization code found in input")

    # Use explicitly provided state, or state detected from the input
    state = req.state or detected_state
    verifier = pop_pkce_session(state)
    if not verifier:
        raise HTTPException(
            status_code=400,
            detail="PKCE session not found or expired. Please restart the OAuth flow.",
        )

    try:
        cred: OAuthCredential = await exchange_code(code, verifier)
    except Exception as exc:
        log.warning("oauth complete: token exchange failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"Token exchange failed: {exc}") from exc

    pid = store.add_profile(cred)
    data = store.load()
    log.info("oauth: profile saved pid=%s", pid)
    return _profile_summary(pid, data.profiles[pid], data.usageStats)


# ── Profile management ─────────────────────────────────────────────────────────

@router.get("/auth/status")
async def auth_status() -> Dict[str, Any]:
    store = get_store()
    data = store.load()
    summaries: List[ProfileSummary] = []
    for pid, raw in data.profiles.items():
        summaries.append(_profile_summary(pid, raw, data.usageStats))
    return {
        "profiles": [s.model_dump() for s in summaries],
        "count": len(summaries),
    }


@router.post("/auth/test/{profile_id}")
async def test_profile(profile_id: str) -> Dict[str, Any]:
    store = get_store()
    data = store.load()
    if profile_id not in data.profiles:
        raise HTTPException(status_code=404, detail="Profile not found")

    raw = data.profiles[profile_id]
    cred_type = raw.get("type")

    if cred_type == "api_key" and raw.get("provider") == "anthropic":
        from auth.anthropic_auth import test_api_key
        ok = await test_api_key(raw["key"])
        return {"ok": ok, "detail": "Anthropic API key accepted" if ok else "Anthropic API key rejected"}

    if cred_type == "oauth":
        now_ms = int(time.time() * 1000)
        is_expired = raw.get("expires", 0) < now_ms
        return {
            "ok": not is_expired,
            "detail": "Token valid (not expired)" if not is_expired else "Token expired",
        }

    if cred_type == "token":
        now_ms = int(time.time() * 1000)
        expires = raw.get("expires")
        if expires and expires < now_ms:
            return {"ok": False, "detail": "Token expired"}
        return {"ok": True, "detail": "Token present (expiry not checked)"}

    return {"ok": True, "detail": f"Profile type={cred_type} — no live test available"}


@router.delete("/auth/profile/{profile_id}")
async def delete_profile(profile_id: str) -> Dict[str, Any]:
    store = get_store()
    removed = store.remove_profile(profile_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Profile not found")
    return {"ok": True, "profileId": profile_id}
