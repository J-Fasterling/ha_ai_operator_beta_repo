"""Pydantic models shared across the application."""
from __future__ import annotations

import time
import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


# ── LLM wire types (OpenAI-compatible) ────────────────────────────────────────

class Role(str, Enum):
    system = "system"
    user = "user"
    assistant = "assistant"
    tool = "tool"


class ChatMessage(BaseModel):
    role: Role
    content: Optional[str] = None
    name: Optional[str] = None
    tool_call_id: Optional[str] = None
    tool_calls: Optional[list[dict[str, Any]]] = None


class ChatCompletionRequest(BaseModel):
    model: str = "ha-agent"
    messages: list[ChatMessage]
    temperature: Optional[float] = 0.7
    max_tokens: Optional[int] = None
    stream: Optional[bool] = False
    # Ignored when forwarded to internal agent; passed through to upstream LLM.
    tools: Optional[list[dict[str, Any]]] = None
    tool_choice: Optional[Any] = None


class ChatCompletionChoice(BaseModel):
    index: int = 0
    message: ChatMessage
    finish_reason: str = "stop"


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatCompletionResponse(BaseModel):
    id: str = Field(default_factory=lambda: f"chatcmpl-{uuid.uuid4().hex[:16]}")
    object: str = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str = "ha-agent"
    choices: list[ChatCompletionChoice]
    usage: Usage = Field(default_factory=Usage)


# ── Agent / policy types ───────────────────────────────────────────────────────

class RiskLevel(str, Enum):
    read = "read"
    low = "low"
    medium = "medium"
    high = "high"


class PlannedAction(BaseModel):
    tool: str
    params: dict[str, Any] = Field(default_factory=dict)
    risk: RiskLevel = RiskLevel.read
    description: str = ""


class AgentPlan(BaseModel):
    actions: list[PlannedAction]
    reasoning: str = ""
    requires_confirmation: bool = False
    confirmation_token: Optional[str] = None


# ── Audit log ─────────────────────────────────────────────────────────────────

class AuditLogEntry(BaseModel):
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    actor: str = "agent"
    mode: str
    tool: str
    params_summary: str
    result_summary: str
    risk: str
    confirmed: bool = False
