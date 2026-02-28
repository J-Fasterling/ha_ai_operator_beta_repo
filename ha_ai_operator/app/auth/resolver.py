"""Token resolver: picks the best credential for a provider and refreshes OAuth tokens.

Resolution order:
  1. Explicit profile_id (if given)
  2. lastGood[provider]
  3. First entry in order[provider]

Never call this with asyncio.run() — it must run inside the Uvicorn event loop.
The sync fallback in llm_clients.py reads the store directly without refresh logic.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from auth.store import AuthStore, OAuthCredential, TokenCredential, ApiKeyCredential

log = logging.getLogger("ha_ai_operator.auth.resolver")

# Refresh tokens 5 minutes before expiry
_REFRESH_WINDOW_MS = 5 * 60 * 1000

# Guard against concurrent refresh of the same profile
_refreshing: set[str] = set()


class NeedsReauthError(Exception):
    """Raised when a credential is expired and cannot be auto-refreshed."""


async def resolve_token(
    provider: str,
    profile_id: Optional[str],
    store: AuthStore,
) -> str:
    """Return the bearer token / API key for the given provider.

    Raises NeedsReauthError if no usable credential is found.
    """
    data = store.load()

    # Build candidate list
    candidates: list[str] = []
    if profile_id and profile_id in data.profiles:
        candidates.append(profile_id)
    last_good = data.lastGood.get(provider)
    if last_good and last_good in data.profiles and last_good not in candidates:
        candidates.append(last_good)
    for pid in data.order.get(provider, []):
        if pid in data.profiles and pid not in candidates:
            candidates.append(pid)

    if not candidates:
        raise NeedsReauthError(f"No credentials found for provider={provider}")

    for pid in candidates:
        raw = data.profiles[pid]
        cred_type = raw.get("type")

        if cred_type == "api_key":
            cred = ApiKeyCredential.model_validate(raw)
            store.set_last_good(provider, pid)
            return cred.key

        elif cred_type == "token":
            cred = TokenCredential.model_validate(raw)
            now_ms = int(time.time() * 1000)
            if cred.expires and cred.expires < now_ms:
                log.warning("resolver: token %s expired", pid)
                mark_profile_failed(pid, "expired", store)
                continue
            store.set_last_good(provider, pid)
            return cred.token

        elif cred_type == "oauth":
            cred = OAuthCredential.model_validate(raw)
            now_ms = int(time.time() * 1000)

            if cred.expires < now_ms:
                # Expired — attempt blocking refresh
                try:
                    cred = await _do_refresh(pid, cred, store)
                except Exception as exc:
                    log.warning("resolver: oauth refresh failed for %s: %s", pid, exc)
                    mark_profile_failed(pid, str(exc), store)
                    continue
            elif cred.expires - now_ms < _REFRESH_WINDOW_MS:
                # Within 5-min window — schedule background refresh
                if pid not in _refreshing:
                    asyncio.create_task(_bg_refresh(pid, cred, store))

            store.set_last_good(provider, pid)
            return cred.access

    raise NeedsReauthError(
        f"All credentials for provider={provider} are expired or invalid"
    )


async def _do_refresh(
    pid: str, cred: OAuthCredential, store: AuthStore
) -> OAuthCredential:
    """Synchronous (blocking) OAuth refresh."""
    from auth.oauth_openai import refresh_token
    new_cred = await refresh_token(cred)
    store.update_oauth_tokens(
        pid, new_cred.access, new_cred.refresh, new_cred.expires
    )
    mark_profile_success(pid, store)
    return new_cred


async def _bg_refresh(pid: str, cred: OAuthCredential, store: AuthStore) -> None:
    """Background (fire-and-forget) OAuth refresh."""
    if pid in _refreshing:
        return
    _refreshing.add(pid)
    try:
        await _do_refresh(pid, cred, store)
        log.info("resolver: background refresh done for %s", pid)
    except Exception as exc:
        log.warning("resolver: background refresh failed for %s: %s", pid, exc)
    finally:
        _refreshing.discard(pid)


# ── Helpers ───────────────────────────────────────────────────────────────────

def mark_profile_failed(pid: str, reason: str, store: AuthStore) -> None:
    data = store.load()
    stats = data.usageStats.get(pid)
    error_count = (stats.errorCount + 1) if stats else 1
    failure_counts = dict(stats.failureCounts) if stats else {}
    failure_counts[reason] = failure_counts.get(reason, 0) + 1
    store.update_usage_stats(
        pid,
        errorCount=error_count,
        failureCounts=failure_counts,
    )


def mark_profile_success(pid: str, store: AuthStore) -> None:
    now_ms = int(time.time() * 1000)
    store.update_usage_stats(pid, lastUsed=now_ms, errorCount=0)
