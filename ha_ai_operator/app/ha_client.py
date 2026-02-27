"""Home Assistant & Supervisor API client.

All requests are routed through the internal Supervisor proxy and authenticated
with the SUPERVISOR_TOKEN environment variable injected by the HA Supervisor.

REST base URL  : http://supervisor/core/api/
Supervisor URL : http://supervisor/
Auth header    : Authorization: Bearer <SUPERVISOR_TOKEN>
"""
from __future__ import annotations

import os
from typing import Any, Optional

import httpx

_HA_API = "http://supervisor/core/api"
_SUP_API = "http://supervisor"


def _token() -> str:
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        raise RuntimeError(
            "SUPERVISOR_TOKEN is not set. "
            "Make sure homeassistant_api: true is in config.yaml."
        )
    return token


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_token()}",
        "Content-Type": "application/json",
    }


# ── Home Assistant API ────────────────────────────────────────────────────────

async def ha_get_state(entity_id: str) -> dict[str, Any]:
    """Return the current state object for *entity_id*."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(f"{_HA_API}/states/{entity_id}", headers=_headers())
        r.raise_for_status()
        return r.json()


async def ha_list_entities(domain: Optional[str] = None) -> list[dict[str, Any]]:
    """Return all states, optionally filtered to *domain* (e.g. 'light')."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(f"{_HA_API}/states", headers=_headers())
        r.raise_for_status()
        states: list[dict] = r.json()
    if domain:
        prefix = f"{domain}."
        states = [s for s in states if s.get("entity_id", "").startswith(prefix)]
    return states


async def ha_call_service(
    domain: str, service: str, service_data: dict[str, Any]
) -> dict[str, Any]:
    """Call a HA service.  Returns the list of affected states (may be empty)."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(
            f"{_HA_API}/services/{domain}/{service}",
            headers=_headers(),
            json=service_data,
        )
        r.raise_for_status()
        return r.json() if r.content else {}


async def ha_get_config() -> dict[str, Any]:
    """Return the HA core configuration object."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(f"{_HA_API}/config", headers=_headers())
        r.raise_for_status()
        return r.json()


async def ha_get_services() -> list[dict[str, Any]]:
    """Return all registered HA services."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(f"{_HA_API}/services", headers=_headers())
        r.raise_for_status()
        return r.json()


# ── Supervisor API (only when allow_supervisor_api == true) ───────────────────

async def supervisor_host_info() -> dict[str, Any]:
    """Return host hardware/OS information from the Supervisor."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(f"{_SUP_API}/host/info", headers=_headers())
        r.raise_for_status()
        return r.json()


async def supervisor_core_info() -> dict[str, Any]:
    """Return HA Core version / state information from the Supervisor."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(f"{_SUP_API}/core/info", headers=_headers())
        r.raise_for_status()
        return r.json()


async def supervisor_restart_core() -> dict[str, Any]:
    """VERY HIGH RISK — restart HA Core via the Supervisor."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(f"{_SUP_API}/core/restart", headers=_headers())
        r.raise_for_status()
        return r.json() if r.content else {"result": "restart requested"}
