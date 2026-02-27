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
   - `llm_base_url` — URL of your LLM API (leave empty for OpenAI default)
   - `llm_api_key` — your API key (not needed for local Ollama)
   - `mode` — start with `read_only`, upgrade to `control_assist` or `ops_write` when ready
6. Click **Start** and then open the **HA AI Operator** panel in the sidebar.

## Quick start (Ollama example)

```yaml
# ha_ai_operator/options in config
timezone: "Europe/Berlin"
mode: "control_assist"
llm_provider: "ollama"
llm_base_url: "http://192.168.1.50:11434/v1"
llm_api_key: null
confirmation_required: true
max_actions_per_turn: 5
audit_log_level: "minimal"
```

## Security

Read [SECURITY.md](SECURITY.md) before enabling `ops_write` mode or `allow_supervisor_api`.

## Documentation

Full configuration reference and examples are in [DOCS.md](DOCS.md).
