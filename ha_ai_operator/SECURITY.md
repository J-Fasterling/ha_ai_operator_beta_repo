# Security Policy — HA AI Operator

## Threat model

### 1. Prompt injection

**Risk**: A malicious entity name, automation description, or calendar event could
inject instructions into the LLM context and cause the agent to execute unintended
HA service calls.

**Mitigations**:
- **`read_only` default mode** — no write operations are possible at all.
- **`confirmation_required: true`** — every medium/high-risk action requires an
  explicit `CONFIRM:<token>` reply from the operator; a passive injection cannot
  produce that reply without human involvement.
- **Policy denylist** — services like `alarm_control_panel/alarm_disarm` and
  `lock/unlock` are on a permanent denylist that cannot be overridden by the LLM.
- **Risk classification** — even in `ops_write`, the policy engine re-classifies
  every tool call independently of what the LLM claims.

**Residual risk**: An attacker with write access to HA entity state (e.g. via
another compromised device) could craft payloads. Keep untrusted integrations
isolated.

### 2. API key / token exposure

**Risk**: The Codex OAuth token or `SUPERVISOR_TOKEN` leaks via logs, the audit trail,
or error responses.

**Mitigations**:
- `run.sh` never logs secret values. The log explicitly states only whether a token
  *is set*, not its value or length.
- `agent.py` filters parameter summaries to exclude keys named `api_key`, `token`,
  `password`, `secret` before writing audit entries.
- FastAPI error responses do not echo request bodies.

**Best practices**:
- Do not expose the add-on's Ingress port directly to the internet.
- Use HA's built-in remote access (Nabu Casa / VPN) rather than port-forwarding.
- Revoke/recreate your Codex login if you suspect OAuth token exposure.

### 3. Privilege escalation via Supervisor API

**Risk**: `allow_supervisor_api: true` + `supervisor_restart_core` gives the agent
the ability to restart HA, causing downtime.

**Mitigations**:
- `allow_supervisor_api` defaults to `false`.
- `supervisor_restart_core` is classified as `high` risk and blocked in all modes
  except `ops_write`. Even then, it requires `confirmation_required: true`.
- The add-on does **not** enable `hassio_api: true` in its manifest by default,
  which would grant direct Supervisor socket access.

**Recommendation**: Leave `allow_supervisor_api: false` unless you have a specific
operational need.

### 4. Supply chain / image integrity

**Risk**: A compromised base image or Python dependency introduces malicious code.

**Mitigations**:
- `build.yaml` pins the HA base image to a specific minor version tag.
- `requirements.txt` pins all Python dependencies to exact versions.
- Rebuild the image regularly to pick up security patches.
- Review dependency changelogs before upgrading.

**Recommendations**:
- Enable [Dependabot](https://docs.github.com/en/code-security/dependabot) on your
  fork to receive automated dependency update PRs.
- Use Docker content trust or image digest pinning in CI/CD.

### 5. Runaway agent / denial of service

**Risk**: A misbehaving LLM issues many tool calls in a loop, hammering the HA API
or consuming LLM quota.

**Mitigation**: `max_actions_per_turn` (default 5) hard-limits tool calls per
response. Adjust with care.

---

## Reporting vulnerabilities

Please do **not** open a public GitHub issue for security vulnerabilities.
Instead, contact the maintainer privately at: `https://github.com/J-Fasterling/ha_ai_operator/security/advisories/new`
or open a GitHub Security Advisory at the repository URL.

---

## Checklist before going to `ops_write`

- [ ] HA is running behind HTTPS (Nabu Casa, reverse proxy, or VPN).
- [ ] `confirmation_required: true` is set.
- [ ] `allow_supervisor_api` is only enabled if you need it.
- [ ] You trust the LLM provider and the network path to it.
- [ ] You have reviewed the denylist in `policy.py` and confirmed it covers your
      critical devices.
- [ ] You have read and understood the prompt-injection risk above.
