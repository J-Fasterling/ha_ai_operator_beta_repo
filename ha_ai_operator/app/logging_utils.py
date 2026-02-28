"""Logging helpers for HA AI Operator."""
from __future__ import annotations

import logging
import os
from typing import Any

_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "token",
    "password",
    "secret",
    "authorization",
)

_LOGGING_CONFIGURED = False


def configure_logging() -> None:
    """Initialize process-wide logging once."""
    global _LOGGING_CONFIGURED
    if _LOGGING_CONFIGURED:
        return

    level_name = os.environ.get("APP_LOG_LEVEL", "info").upper()
    level = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger()
    if root.handlers:
        root.setLevel(level)
        for handler in root.handlers:
            handler.setLevel(level)
    else:
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    _LOGGING_CONFIGURED = True


def sanitize_for_log(value: Any) -> Any:
    """Redact secret-ish keys recursively and trim long strings."""
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, child in value.items():
            key_str = str(key)
            if any(part in key_str.lower() for part in _SENSITIVE_KEY_PARTS):
                clean[key_str] = "[REDACTED]"
            else:
                clean[key_str] = sanitize_for_log(child)
        return clean
    if isinstance(value, list):
        return [sanitize_for_log(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_for_log(item) for item in value)
    if isinstance(value, str):
        return value if len(value) <= 400 else value[:400] + "...[truncated]"
    return value
