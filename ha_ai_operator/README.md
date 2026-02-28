# HA AI Operator

An on-device AI agent add-on for [Home Assistant](https://www.home-assistant.io/) that lets you control and inspect your smart home through natural language, with **security-by-default**.

## Features

- **Chat UI** embedded in the HA frontend via Ingress (no custom integration needed)
- **OpenAI-compatible endpoint** (`POST /v1/chat/completions`) for external LLM clients
- **Three operating modes**: `read_only`, `control_assist`, `ops_write`
- **Confirmation gate**: medium/high-risk actions are shown as a plan first; you confirm with a token
- **Audit log**: every tool call is written to `/data/state/audit.jsonl`
- **Pluggable LLM backend**: OpenAI, Ollama, or any OpenAI-compatible API
- **Optional Supervisor tools**: host info, core info, core restart (disabled by default)

## Installation

1. In Home Assistant, go to **Settings → Add-ons → Add-on Store**.
2. Click the three-dot menu (⋮) in the top-right corner and choose **Repositories**.
3. Add the repository URL:
   ```
   https://github.com/J-Fasterling/ha_ai_operator
   ```
4. Find **HA AI Operator** in the store and click **Install**.
5. Go to the **Configuration** tab and set at minimum:
   - `llm_provider` — choose your LLM backend
   - `openai_auth_mode` — `api_key` (default) or `codex_oauth`
   - `llm_base_url` — URL of your LLM API (leave empty for OpenAI default)
   - `llm_api_key` — your API key (when `openai_auth_mode: api_key`)
   - `llm_oauth_token` — your ChatGPT Codex OAuth token (when `openai_auth_mode: codex_oauth`)
   - `mode` — start with `read_only`, upgrade to `control_assist` or `ops_write` when ready
6. Click **Start** and then open the **HA AI Operator** panel in the sidebar.

## Codex OAuth token quick guide

`codex_oauth` currently works as a **manual token paste** flow (no browser redirect inside Home Assistant yet).

1. On your computer, log in once with Codex CLI:
   ```bash
   codex login
   ```
2. Read the current access token from local Codex auth storage:
   ```bash
   jq -r '.tokens.access_token' ~/.codex/auth.json
   ```
3. In Home Assistant add-on config, set:
   - `openai_auth_mode: "codex_oauth"`
   - `llm_oauth_token: "<paste token here>"`
4. Restart the add-on.

Important: this access token can expire. If requests start failing with auth errors, fetch a fresh token and update `llm_oauth_token`.

## Quick start (Ollama example)

```yaml
# ha_ai_operator/options in config
timezone: "Europe/Berlin"
mode: "control_assist"
llm_provider: "ollama"
openai_auth_mode: "api_key"
llm_base_url: "http://192.168.1.50:11434/v1"
llm_api_key: null
llm_oauth_token: null
confirmation_required: true
max_actions_per_turn: 5
audit_log_level: "minimal"
app_log_level: "info"
```

## Security

Read [SECURITY.md](SECURITY.md) before enabling `ops_write` mode or `allow_supervisor_api`.

## Documentation

Full configuration reference and examples are in [DOCS.md](DOCS.md).
