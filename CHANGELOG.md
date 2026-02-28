# Changelog

All notable changes to HA AI Operator are documented here.
Versions on the `beta` remote are test releases; `origin/main` carries stable releases.

---

## [0.1.1-beta.6] – 2026-02-28

### Fixed
- **Ingress UI diagnostics/startup script failed to execute** — corrected escaping in
  embedded JavaScript (regex/newline sequences) so the script parses in-browser.
  This unblocks `loadStatus()`/`/health` requests and frontend log forwarding.

---

## [0.1.1-beta.5] – 2026-02-28

### Added
- **End-to-end startup diagnostics for Ingress UI loading issues**:
  - Structured backend request logging with request IDs, status codes, and durations.
  - New `POST /api/frontend-log` endpoint so browser-side events are written into add-on logs.
  - Frontend diagnostics panel showing recent boot/load events directly in the UI.
  - Frontend error hooks (`window.onerror`, `unhandledrejection`) plus traced health fetch logging.
- New add-on option `app_log_level` (`debug|info|warning|error`) to control backend logging verbosity.

### Changed
- `run.sh` now starts uvicorn with the configured `app_log_level` and keeps access logging enabled.
- `/health` now also reports `app_log_level` for easier runtime verification.

---

## [0.1.1-beta.4] – 2026-02-28

### Fixed
- **Ingress API routing regression in chat UI** — reverted from bare relative
  `fetch('health')`/`fetch('api/audit')` calls to explicit `apiBase` resolution.
  The previous approach broke when HA served the panel under `/<slug>` (no
  trailing slash), causing requests to resolve to wrong paths and leaving the UI
  stuck at "Loading configuration…".
- Added robust base-path detection for both URL shapes:
  - `/api/hassio_ingress/<slug>/...`
  - `/<slug>` (mapped to `/api/hassio_ingress/<slug>/...`)
- Health error output now includes the resolved request URL to speed up ingress
  diagnostics.

---

## [0.1.1-beta.3] – 2026-02-28

### Fixed
- **UI never updated past "Loading configuration…"** — replaced `detectApiBase()` with
  bare relative `fetch('health')` URLs. The old function constructed absolute paths
  (`/api/hassio_ingress/TOKEN/health`) that silently failed whenever
  `window.location.pathname` did not match the ingress regex (direct container access,
  reverse-proxy setups). Browser-native relative URL resolution is correct in all cases.
- `loadStatus()` now shows a visible **OFFLINE** badge + error text instead of
  swallowing exceptions with `catch(e) { /* ignore */ }`, making failures diagnosable.

### Changed
- `storage.py`: data and app directories are now configurable via `DATA_DIR` / `APP_DIR`
  environment variables, enabling local testing without a running HA instance.

---

## [0.1.1-beta.2] – 2026-02-27

### Added
- **Anthropic Claude support** (`llm_provider: anthropic`) — full wire-format adapter in
  `llm_clients.py`: converts OpenAI tool-call format ↔ Anthropic Messages API
  (`/v1/messages`, `x-api-key` header, `tool_use`/`tool_result` content blocks,
  temperature clamped to 0–1).
- **OAuth / Codex token auth** (`openai_auth_mode: codex_oauth`) — use a short-lived
  OAuth Bearer token instead of a static API key for OpenAI-compatible providers.
  New config options: `openai_auth_mode`, `llm_oauth_token`.
- **Soul / character file** — agent personality loaded from `/data/soul.md` (user
  override) with fallback to `/app/default_soul.md` (bundled). Describes identity,
  values, tone, and behavioural rules.
- `storage.py`: `load_soul()` helper with two-path resolution.
- Ingress UI: `detectApiBase()` for dynamic API base-URL resolution (superseded in
  beta.3).

### Changed
- `llm_clients.py` extracted from `agent.py`; `make_llm_client()` factory selects
  `OpenAICompatibleClient` or `AnthropicClient` from `LLM_PROVIDER` env var.
- `run.sh`: reads and exports `OPENAI_AUTH_MODE`, `LLM_OAUTH_TOKEN`.
- `config.yaml`: `llm_provider` enum extended with `anthropic`.
- Translations (en/de) updated with new option descriptions.

---

## [0.1.0] – 2026-02-27  *(initial release)*

### Added
- HA Add-on scaffold: `config.yaml`, `Dockerfile`, `build.yaml`, `run.sh`.
- FastAPI application with endpoints:
  - `GET /health` — JSON status
  - `POST /v1/chat/completions` — OpenAI-compatible agent endpoint
  - `GET /api/audit?limit=N` — audit log
  - `GET /debug/selftest` — connectivity check
  - `GET /` — Ingress chat UI
- Agent loop with OpenAI tool-calling, confirmation-token gate for risky actions.
- Policy engine: risk levels `read / low / medium / high`, mode-gated access, denylist
  for `alarm_disarm`, `lock/unlock`, `cover/open_cover`.
- Persistent audit log at `/data/state/audit.jsonl`.
- Three operating modes: `read_only`, `control_assist`, `ops_write`.
- LLM backends: `openai_compatible`, `ollama`, `custom_http`.
- Translations: `en`, `de`.
- `README.md`, `DOCS.md`, `SECURITY.md`.
