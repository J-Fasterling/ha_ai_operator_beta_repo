"""Policy / risk engine for the HA AI Operator.

Risk hierarchy:  read < low < medium < high

Mode capabilities:
  read_only      – only tools classified as `read`
  control_assist – read + low-risk actions (lights, media, input_boolean …)
  ops_write      – all actions; medium/high require confirmation if
                   confirmation_required == true
"""
from __future__ import annotations

import os
from typing import Optional

from schemas import PlannedAction, RiskLevel

# ── Denylist: always blocked regardless of mode ───────────────────────────────
# These services are *never* executed without explicit ops_write mode AND
# the add-on user explicitly enabling them.
_DENYLIST_ABSOLUTE: frozenset[tuple[str, str]] = frozenset(
    {
        # Disarm alarm without confirmation (e.g., without a code)
        ("alarm_control_panel", "alarm_disarm"),
        # Unlock a lock
        ("lock", "unlock"),
        # Open a garage door / gate
        ("cover", "open_cover"),
    }
)

# ── Service → risk classification ─────────────────────────────────────────────
_SERVICE_RISK: dict[tuple[str, str], RiskLevel] = {
    # Lights — low
    ("light", "turn_on"): RiskLevel.low,
    ("light", "turn_off"): RiskLevel.low,
    ("light", "toggle"): RiskLevel.low,
    # Media — low
    ("media_player", "turn_on"): RiskLevel.low,
    ("media_player", "turn_off"): RiskLevel.low,
    ("media_player", "play_media"): RiskLevel.low,
    ("media_player", "media_pause"): RiskLevel.low,
    # Input helpers — low
    ("input_boolean", "turn_on"): RiskLevel.low,
    ("input_boolean", "turn_off"): RiskLevel.low,
    ("input_boolean", "toggle"): RiskLevel.low,
    ("input_number", "set_value"): RiskLevel.low,
    ("input_select", "select_option"): RiskLevel.low,
    # Switches — medium (could control appliances, smart plugs, etc.)
    ("switch", "turn_on"): RiskLevel.medium,
    ("switch", "turn_off"): RiskLevel.medium,
    ("switch", "toggle"): RiskLevel.medium,
    # Climate — medium
    ("climate", "set_temperature"): RiskLevel.medium,
    ("climate", "turn_on"): RiskLevel.medium,
    ("climate", "turn_off"): RiskLevel.medium,
    ("climate", "set_hvac_mode"): RiskLevel.medium,
    # Scripts / automations — medium
    ("script", "turn_on"): RiskLevel.medium,
    ("automation", "trigger"): RiskLevel.medium,
    ("automation", "turn_on"): RiskLevel.medium,
    ("automation", "turn_off"): RiskLevel.medium,
    # Notifications — low
    ("notify", "notify"): RiskLevel.low,
    # HA core — medium
    ("homeassistant", "restart"): RiskLevel.high,
    ("homeassistant", "stop"): RiskLevel.high,
    ("homeassistant", "check_config"): RiskLevel.low,
    # Security — high (see also _DENYLIST_ABSOLUTE)
    ("alarm_control_panel", "alarm_disarm"): RiskLevel.high,
    ("alarm_control_panel", "alarm_arm_home"): RiskLevel.medium,
    ("alarm_control_panel", "alarm_arm_away"): RiskLevel.medium,
    ("lock", "unlock"): RiskLevel.high,
    ("lock", "lock"): RiskLevel.medium,
    ("cover", "open_cover"): RiskLevel.high,
    ("cover", "close_cover"): RiskLevel.medium,
}

# ── Supervisor tool risk ───────────────────────────────────────────────────────
_SUPERVISOR_RISK: dict[str, RiskLevel] = {
    "supervisor_host_info": RiskLevel.read,
    "supervisor_core_info": RiskLevel.read,
    "supervisor_restart_core": RiskLevel.high,
}

# ── Read-only tools ────────────────────────────────────────────────────────────
_READ_TOOLS: frozenset[str] = frozenset(
    {
        "ha_get_state",
        "ha_list_entities",
        "ha_get_config",
        "ha_get_services",
        "supervisor_host_info",
        "supervisor_core_info",
    }
)


def classify_risk(tool: str, params: dict) -> RiskLevel:
    """Return the risk level for a given tool call."""
    if tool in _READ_TOOLS:
        return RiskLevel.read
    if tool in _SUPERVISOR_RISK:
        return _SUPERVISOR_RISK[tool]
    if tool == "ha_call_service":
        domain = params.get("domain", "")
        service = params.get("service", "")
        return _SERVICE_RISK.get((domain, service), RiskLevel.medium)
    return RiskLevel.medium


def is_denylisted(tool: str, params: dict) -> bool:
    """Return True if the action is on the absolute denylist."""
    if tool == "ha_call_service":
        domain = params.get("domain", "")
        service = params.get("service", "")
        return (domain, service) in _DENYLIST_ABSOLUTE
    return False


def is_supervisor_tool(tool: str) -> bool:
    return tool.startswith("supervisor_")


# ── Policy engine ─────────────────────────────────────────────────────────────

class PolicyEngine:
    """Stateless policy checker; reads config from environment on every call."""

    def __init__(self) -> None:
        self.mode: str = os.environ.get("MODE", "read_only")
        self.confirmation_required: bool = (
            os.environ.get("CONFIRMATION_REQUIRED", "true").lower() == "true"
        )
        self.max_actions: int = int(os.environ.get("MAX_ACTIONS_PER_TURN", "5"))
        self.allow_supervisor: bool = (
            os.environ.get("ALLOW_SUPERVISOR_API", "false").lower() == "true"
        )

    # ── single-action checks ──────────────────────────────────────────────────

    def check_action(self, tool: str, params: dict) -> tuple[bool, str]:
        """Return (allowed, reason).  *reason* is 'ok' when allowed."""
        # Supervisor guard
        if is_supervisor_tool(tool) and not self.allow_supervisor:
            return False, (
                f"Supervisor tool '{tool}' is disabled. "
                "Set allow_supervisor_api: true to enable it."
            )

        # Denylist check
        if is_denylisted(tool, params):
            if self.mode != "ops_write":
                return False, (
                    f"Tool '{tool}' is on the permanent denylist and requires "
                    "ops_write mode."
                )
            # In ops_write: allowed but must be confirmed (handled at plan level)

        risk = classify_risk(tool, params)

        match self.mode:
            case "read_only":
                if risk != RiskLevel.read:
                    return False, (
                        f"Tool '{tool}' has risk '{risk.value}' but mode is read_only."
                    )
                return True, "ok"

            case "control_assist":
                if risk == RiskLevel.high:
                    return False, (
                        f"Tool '{tool}' has risk 'high' which is not allowed "
                        "in control_assist mode."
                    )
                return True, "ok"

            case "ops_write":
                return True, "ok"

        return False, f"Unknown mode: {self.mode}"

    # ── plan-level checks ─────────────────────────────────────────────────────

    def plan_requires_confirmation(self, actions: list[PlannedAction]) -> bool:
        """Return True if at least one action in *actions* warrants confirmation."""
        if not self.confirmation_required:
            return False
        if self.mode == "read_only":
            return False
        for action in actions:
            if action.risk in (RiskLevel.medium, RiskLevel.high):
                return True
            if is_denylisted(action.tool, action.params):
                return True
        return False

    def max_actions_count(self) -> int:
        return self.max_actions
