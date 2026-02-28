"""Anthropic credential helpers."""
from __future__ import annotations

import logging

import httpx

from auth.store import ApiKeyCredential, AuthStore, TokenCredential

log = logging.getLogger("ha_ai_operator.auth.anthropic")

_ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models"
_ANTHROPIC_VERSION = "2023-06-01"


def add_api_key(key: str, store: AuthStore) -> str:
    """Validate and store an Anthropic API key; returns profileId."""
    key = key.strip()
    if not key:
        raise ValueError("API key must not be empty")
    cred = ApiKeyCredential(provider="anthropic", key=key)
    return store.add_profile(cred)


def add_setup_token(token: str, expires_ms: int | None, store: AuthStore) -> str:
    """Store an Anthropic setup/bearer token; returns profileId."""
    token = token.strip()
    if not token:
        raise ValueError("Token must not be empty")
    cred = TokenCredential(provider="anthropic", token=token, expires=expires_ms)
    return store.add_profile(cred)


async def test_api_key(key: str) -> bool:
    """Return True if the key is accepted by Anthropic's /v1/models endpoint."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                _ANTHROPIC_MODELS_URL,
                headers={
                    "x-api-key": key,
                    "anthropic-version": _ANTHROPIC_VERSION,
                },
            )
        return r.status_code == 200
    except Exception as exc:
        log.debug("anthropic test_api_key failed: %s", exc)
        return False
