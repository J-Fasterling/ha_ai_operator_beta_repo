#!/usr/bin/with-contenv bashio
# shellcheck shell=bash
set -euo pipefail

bashio::log.info "=== HA AI Operator starting ==="

# ── Read required options ────────────────────────────────────────────────────
export TIMEZONE="$(bashio::config 'timezone')"
export MODE="$(bashio::config 'mode')"
export LLM_PROVIDER="$(bashio::config 'llm_provider')"
export ALLOW_SUPERVISOR_API="$(bashio::config 'allow_supervisor_api')"
export CONFIRMATION_REQUIRED="$(bashio::config 'confirmation_required')"
export MAX_ACTIONS_PER_TURN="$(bashio::config 'max_actions_per_turn')"
export AUDIT_LOG_LEVEL="$(bashio::config 'audit_log_level')"

# ── Handle nullable / optional secrets ───────────────────────────────────────
# Never log the values of secret fields.
if bashio::config.has_value 'llm_base_url'; then
    export LLM_BASE_URL
    LLM_BASE_URL="$(bashio::config 'llm_base_url')"
else
    export LLM_BASE_URL=""
fi

if bashio::config.has_value 'llm_api_key'; then
    export LLM_API_KEY
    LLM_API_KEY="$(bashio::config 'llm_api_key')"
    # Never log LLM_API_KEY — not even a hash or length.
else
    export LLM_API_KEY=""
fi

# ── Safe startup log (no secrets) ─────────────────────────────────────────────
bashio::log.info "Mode              : ${MODE}"
bashio::log.info "LLM provider      : ${LLM_PROVIDER}"
bashio::log.info "LLM base URL set  : $([ -n "${LLM_BASE_URL}"  ] && echo yes || echo no)"
bashio::log.info "LLM API key set   : $([ -n "${LLM_API_KEY}"   ] && echo yes || echo no)"
bashio::log.info "Supervisor API    : ${ALLOW_SUPERVISOR_API}"
bashio::log.info "Confirmation req. : ${CONFIRMATION_REQUIRED}"
bashio::log.info "Max actions/turn  : ${MAX_ACTIONS_PER_TURN}"
bashio::log.info "Audit log level   : ${AUDIT_LOG_LEVEL}"

# ── Persistent data directories ───────────────────────────────────────────────
mkdir -p /data/state /data/memory /data/checkpoints

# ── Launch FastAPI ────────────────────────────────────────────────────────────
bashio::log.info "Listening on 0.0.0.0:8099 (Ingress port)"

exec python3 -m uvicorn main:app \
    --host 0.0.0.0 \
    --port 8099 \
    --workers 1 \
    --log-level info \
    --no-access-log
