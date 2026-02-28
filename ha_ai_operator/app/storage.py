"""Persistent storage helpers.

Layout under /data/:
  soul.md                    – optional: user-written agent soul/character file
  state/audit.jsonl          – append-only audit log (one JSON line per entry)
  state/pending_plans.json   – transient: plans awaiting user confirmation
  memory/                    – reserved for future agent memory / RAG
  checkpoints/               – reserved for future agent checkpoints

Soul resolution order
─────────────────────
  1. /data/soul.md           (user override — edit via HA file-editor add-on)
  2. /app/default_soul.md    (bundled default — updated with each image build)
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from schemas import AuditLogEntry

_DATA = Path(os.environ.get("DATA_DIR", "/data"))
_STATE = _DATA / "state"
_MEMORY = _DATA / "memory"
_CKPT = _DATA / "checkpoints"
_AUDIT = _STATE / "audit.jsonl"
_PENDING = _STATE / "pending_plans.json"
_MAX_PENDING = 30  # upper bound on in-memory confirmation tokens


def ensure_dirs() -> None:
    for d in (_STATE, _MEMORY, _CKPT):
        d.mkdir(parents=True, exist_ok=True)


# ── Soul / character file ─────────────────────────────────────────────────────

_USER_SOUL = _DATA / "soul.md"
_DEFAULT_SOUL = Path(os.environ.get("APP_DIR", "/app")) / "default_soul.md"


def load_soul() -> str:
    """Return the agent's soul/character text.

    Checks /data/soul.md first (user override), then falls back to
    /app/default_soul.md (bundled default).  Returns an empty string if
    neither exists (the agent will run with _BASE_SYSTEM only).
    """
    for candidate in (_USER_SOUL, _DEFAULT_SOUL):
        if candidate.exists():
            try:
                return candidate.read_text(encoding="utf-8").strip()
            except OSError:
                pass
    return ""


# ── Audit log ─────────────────────────────────────────────────────────────────

def append_audit(entry: AuditLogEntry) -> None:
    ensure_dirs()
    with _AUDIT.open("a") as fh:
        fh.write(entry.model_dump_json() + "\n")


def read_audit(limit: int = 50) -> list[dict[str, Any]]:
    """Return up to *limit* most-recent audit entries (newest first)."""
    if not _AUDIT.exists():
        return []
    lines = _AUDIT.read_text(encoding="utf-8").splitlines()
    tail = lines[-limit:] if len(lines) > limit else lines
    result: list[dict] = []
    for raw in reversed(tail):
        raw = raw.strip()
        if raw:
            try:
                result.append(json.loads(raw))
            except json.JSONDecodeError:
                pass
    return result


# ── Pending plans (confirmation tokens) ───────────────────────────────────────

def _load_pending() -> dict[str, Any]:
    if not _PENDING.exists():
        return {}
    try:
        return json.loads(_PENDING.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_pending(data: dict[str, Any]) -> None:
    ensure_dirs()
    _PENDING.write_text(json.dumps(data, indent=2), encoding="utf-8")


def save_pending_plan(token: str, plan: dict[str, Any]) -> None:
    pending = _load_pending()
    pending[token] = {
        "plan": plan,
        "created_at": datetime.utcnow().isoformat(),
    }
    # Evict oldest entries when limit exceeded
    if len(pending) > _MAX_PENDING:
        by_age = sorted(pending, key=lambda k: pending[k].get("created_at", ""))
        for old in by_age[: len(pending) - _MAX_PENDING]:
            del pending[old]
    _save_pending(pending)


def get_pending_plan(token: str) -> Optional[dict[str, Any]]:
    return _load_pending().get(token, {}).get("plan")


def delete_pending_plan(token: str) -> None:
    pending = _load_pending()
    if token in pending:
        del pending[token]
        _save_pending(pending)
