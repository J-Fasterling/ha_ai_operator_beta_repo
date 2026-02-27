# HA AI Operator — Configuration & Usage

## Configuration reference

| Option | Type | Default | Description |
|---|---|---|---|
| `timezone` | string | `Europe/Berlin` | IANA timezone for the container |
| `mode` | enum | `read_only` | Operating mode (see below) |
| `llm_provider` | enum | `openai_compatible` | LLM backend |
| `openai_auth_mode` | enum | `api_key` | `api_key` or `codex_oauth` (OpenAI-compatible only) |
| `llm_base_url` | string? | *(empty)* | LLM API base URL |
| `llm_api_key` | password? | *(empty)* | LLM API key — never logged |
| `llm_oauth_token` | password? | *(empty)* | OAuth bearer token for `codex_oauth` mode |
| `allow_supervisor_api` | bool | `false` | Expose Supervisor tools to agent |
| `confirmation_required` | bool | `true` | Ask before executing risky actions |
| `max_actions_per_turn` | int | `5` | Max HA calls per agent response |
| `audit_log_level` | enum | `minimal` | `minimal` or `verbose` |

---

## Operating modes

### `read_only` (default)

The agent can **only read** data — states, config, entity lists.
No service calls are ever executed.
Ideal for: dashboards, voice queries, status checks.

### `control_assist`

Allows **low-risk control actions**:
- Lights (`turn_on`, `turn_off`, `toggle`)
- Media players (play, pause, stop)
- Input helpers (`input_boolean`, `input_number`, `input_select`)

High-risk actions (security, locks, alarms) are blocked.

### `ops_write`

**Full operator access.**
Medium and high-risk actions (switches, climate, scripts, locks, alarms) require
user confirmation unless `confirmation_required: false`.

> Do not run `ops_write` unattended or with an untrusted LLM provider.

---

## Confirmation flow

When the agent proposes a risky action, it returns a plan like:

```
I have prepared the following plan:

  1. **ha_call_service**({"domain":"switch","service":"turn_off",...}) [risk: medium]

To confirm, reply exactly:
CONFIRM:a1b2c3d4e5f6g7h8i9j0
```

Reply with `CONFIRM:<token>` to execute.
Reply with anything else (or just a new instruction) to cancel.
Tokens expire if the server restarts (they are stored in `/data/state/pending_plans.json`).

---

## LLM provider setup

### OpenAI / Groq / Together / OpenRouter

```yaml
llm_provider: "openai_compatible"
openai_auth_mode: "api_key"
llm_base_url: "https://api.groq.com/openai/v1"   # example: Groq
llm_api_key: "gsk_…"
llm_oauth_token: null
```

### ChatGPT Codex OAuth (OpenAI-compatible)

```yaml
llm_provider: "openai_compatible"
openai_auth_mode: "codex_oauth"
llm_base_url: "https://api.openai.com/v1"
llm_api_key: null
llm_oauth_token: "eyJ..."
```

### How to obtain `llm_oauth_token` (simple manual flow)

There is currently no in-add-on browser redirect for OAuth. Use this manual flow:

1. Login with Codex CLI on your machine:
   ```bash
   codex login
   ```
2. Extract the current OAuth access token:
   ```bash
   jq -r '.tokens.access_token' ~/.codex/auth.json
   ```
3. Paste it into Home Assistant add-on config as `llm_oauth_token`.
4. Restart the add-on.

Note: the access token is temporary. If you see authentication failures, fetch a fresh token and update `llm_oauth_token`.

### Local Ollama

```yaml
llm_provider: "ollama"
openai_auth_mode: "api_key"
llm_base_url: "http://192.168.1.50:11434/v1"
llm_api_key: null
llm_oauth_token: null
```

Recommended models with tool-calling support: `llama3.1`, `mistral-nemo`, `qwen2.5`.

### Custom HTTP endpoint

```yaml
llm_provider: "custom_http"
openai_auth_mode: "api_key"
llm_base_url: "http://my-llm-proxy:8080/v1"
llm_api_key: "my-key"
llm_oauth_token: null
```

The endpoint must implement `POST /chat/completions` with OpenAI tool-call format.

---

## Supervisor API tools

Set `allow_supervisor_api: true` to enable:

| Tool | Risk | Description |
|---|---|---|
| `supervisor_host_info` | read | Host hardware / OS info |
| `supervisor_core_info` | read | HA Core version and state |
| `supervisor_restart_core` | **high** | Restart HA Core (causes downtime) |

Note: this option **does not** enable `hassio_api: true` in the add-on manifest.
If you need the Supervisor socket (e.g. for add-on management), manually edit
`config.yaml` and set `hassio_api: true` and rebuild.

---

## Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | JSON status (mode, provider, flags) |
| `POST` | `/v1/chat/completions` | OpenAI-compatible agent endpoint |
| `GET` | `/api/audit?limit=N` | Last N audit log entries (JSON) |
| `GET` | `/debug/selftest` | Connectivity and config checks |
| `GET` | `/api/docs` | Swagger UI |

---

## Audit log

Every tool call is appended to `/data/state/audit.jsonl` as one JSON line:

```json
{
  "timestamp": "2025-01-15T14:23:01.123456",
  "actor": "agent",
  "mode": "control_assist",
  "tool": "ha_call_service",
  "params_summary": "{\"domain\": \"light\", \"service\": \"turn_off\", ...}",
  "result_summary": "[{\"entity_id\": \"light.kitchen\", ...}]",
  "risk": "low",
  "confirmed": false
}
```

You can tail the log from the HA terminal add-on:

```bash
tail -f /addon_configs/ha_ai_operator/data/state/audit.jsonl | jq
```

---

## Data directory layout

```
/data/
  options.json          ← HA writes your config here
  state/
    audit.jsonl         ← append-only audit log
    pending_plans.json  ← transient confirmation tokens
  memory/               ← reserved for future agent memory
  checkpoints/          ← reserved for future checkpoints
```

---

## Examples

### "Which lights are on?"

Requires: any mode.

```
User: Which lights are currently on?
Agent: [calls ha_list_entities(domain="light")]
       The following lights are on: Living Room, Kitchen Ceiling, Hallway.
```

### "Turn off all lights"

Requires: `control_assist` or `ops_write` + confirmation.

```
User: Turn off all lights please.
Agent: I have prepared the following plan:
         1. ha_call_service({"domain":"light","service":"turn_off",
            "service_data":{"entity_id":"all"}}) [risk: low]
       CONFIRM:abc123…

User: CONFIRM:abc123…
Agent: Executing confirmed plan:
       ✓ ha_call_service → [...]
```

### "What temperature is it outside?"

```
User: What's the outside temperature?
Agent: [calls ha_get_state(entity_id="sensor.outside_temperature")]
       The outside temperature is 12.3 °C.
```
