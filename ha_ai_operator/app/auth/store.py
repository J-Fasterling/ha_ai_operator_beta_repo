"""Persistent credential store.

Stores auth profiles in $DATA_DIR/auth-profiles.json with atomic writes
(mkstemp + os.replace) protected by fcntl.flock.

JSON schema version 1:
{
  "version": 1,
  "profiles": { "<id>": { ... credential fields ... } },
  "order": { "<provider>": ["<id>", ...] },
  "lastGood": { "<provider>": "<id>" },
  "usageStats": { "<id>": { "lastUsed": 0, "cooldownUntil": 0,
                             "errorCount": 0, "failureCounts": {} } }
}
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import tempfile
import uuid
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

log = logging.getLogger("ha_ai_operator.auth.store")

# ── Credential models ──────────────────────────────────────────────────────────

class ApiKeyCredential(BaseModel):
    type: str = "api_key"
    provider: str
    key: str


class TokenCredential(BaseModel):
    type: str = "token"
    provider: str = "anthropic"
    token: str
    expires: Optional[int] = None  # ms epoch; None = no expiry


class OAuthCredential(BaseModel):
    type: str = "oauth"
    provider: str = "openai-codex"
    access: str
    refresh: str
    expires: int  # ms epoch
    accountId: Optional[str] = None
    clientId: Optional[str] = None
    email: Optional[str] = None


AnyCredential = ApiKeyCredential | TokenCredential | OAuthCredential


class UsageStats(BaseModel):
    lastUsed: int = 0
    cooldownUntil: int = 0
    errorCount: int = 0
    failureCounts: Dict[str, int] = Field(default_factory=dict)


class AuthData(BaseModel):
    version: int = 1
    profiles: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    order: Dict[str, List[str]] = Field(default_factory=dict)
    lastGood: Dict[str, str] = Field(default_factory=dict)
    usageStats: Dict[str, UsageStats] = Field(default_factory=dict)


# ── AuthStore ──────────────────────────────────────────────────────────────────

class AuthStore:
    def __init__(self, data_dir: str) -> None:
        self._dir = data_dir
        self._path = os.path.join(data_dir, "auth-profiles.json")
        self._lock_path = os.path.join(data_dir, "auth-profiles.lock")

    # ── Low-level helpers ──────────────────────────────────────────────────────

    def _open_lock(self):
        """Return an open file object for the lock file (caller must close)."""
        return open(self._lock_path, "a")

    def _read_raw(self) -> AuthData:
        """Read and parse the JSON file; return empty AuthData on any error."""
        try:
            with open(self._path) as f:
                raw = json.load(f)
            return AuthData.model_validate(raw)
        except (FileNotFoundError, json.JSONDecodeError, Exception):
            return AuthData()

    def _save_locked(self, data: AuthData) -> None:
        """Atomically write data to the profiles file (must hold lock)."""
        parent = os.path.dirname(self._path)
        fd, tmp = tempfile.mkstemp(dir=parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(data.model_dump_json(indent=2))
            os.replace(tmp, self._path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ── Public API ─────────────────────────────────────────────────────────────

    def load(self) -> AuthData:
        """Read current profiles (no lock needed for reads)."""
        return self._read_raw()

    def add_profile(self, cred: AnyCredential) -> str:
        """Add a new credential profile; returns the new profileId."""
        profile_id = uuid.uuid4().hex[:12]
        with self._open_lock() as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                data = self._read_raw()
                data.profiles[profile_id] = cred.model_dump()
                provider = cred.provider
                if provider not in data.order:
                    data.order[provider] = []
                data.order[provider].append(profile_id)
                data.usageStats[profile_id] = UsageStats()
                self._save_locked(data)
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)
        log.info("auth.store: added profile %s provider=%s type=%s",
                 profile_id, cred.provider, cred.type)
        return profile_id

    def remove_profile(self, profile_id: str) -> bool:
        """Remove a profile by ID; returns True if it existed."""
        with self._open_lock() as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                data = self._read_raw()
                if profile_id not in data.profiles:
                    return False
                cred = data.profiles.pop(profile_id)
                provider = cred.get("provider", "")
                if provider in data.order:
                    data.order[provider] = [
                        pid for pid in data.order[provider] if pid != profile_id
                    ]
                data.usageStats.pop(profile_id, None)
                if data.lastGood.get(provider) == profile_id:
                    del data.lastGood[provider]
                self._save_locked(data)
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)
        log.info("auth.store: removed profile %s", profile_id)
        return True

    def update_oauth_tokens(
        self, profile_id: str, access: str, refresh: str, expires: int
    ) -> None:
        """Patch access/refresh/expires for an OAuth profile."""
        with self._open_lock() as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                data = self._read_raw()
                if profile_id in data.profiles:
                    data.profiles[profile_id]["access"] = access
                    data.profiles[profile_id]["refresh"] = refresh
                    data.profiles[profile_id]["expires"] = expires
                    self._save_locked(data)
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)

    def update_usage_stats(self, profile_id: str, **kwargs: Any) -> None:
        """Merge kwargs into the profile's usageStats."""
        with self._open_lock() as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                data = self._read_raw()
                if profile_id not in data.usageStats:
                    data.usageStats[profile_id] = UsageStats()
                stats = data.usageStats[profile_id]
                for k, v in kwargs.items():
                    if hasattr(stats, k):
                        setattr(stats, k, v)
                self._save_locked(data)
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)

    def set_last_good(self, provider: str, profile_id: str) -> None:
        """Mark profileId as the last known-good for provider."""
        with self._open_lock() as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                data = self._read_raw()
                data.lastGood[provider] = profile_id
                self._save_locked(data)
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)


# ── Singleton ──────────────────────────────────────────────────────────────────

_store: Optional[AuthStore] = None


def get_store() -> AuthStore:
    global _store
    if _store is None:
        data_dir = os.environ.get("DATA_DIR", "/data")
        _store = AuthStore(data_dir)
    return _store
