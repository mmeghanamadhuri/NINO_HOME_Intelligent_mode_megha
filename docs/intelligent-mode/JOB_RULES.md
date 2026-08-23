# Intelligent Mode — Job Rules

These rules are enforced in code (`incident_filters.py`, `orchestrator.py`, `agent_remediation.py`, `verification_agent.py`).

## 1. Detect — when to open an incident

**Open an incident when:**
- A health check fails after grace period.
- Smoke/E2E/soak reports a real failure (`passed=False`, not skipped).
- Live monitoring shows sustained anomaly (camera, bot offline, GPU Ollama down).

**Never open an incident when:**
- Test message contains `skipped` or `skipped=True`.
- Soak deferred: live voice session active.
- Error is CPU Ollama `:11434` only and GPU `:11435` is healthy.
- Wake reject or other benign voice latency rows.
- Agent benign pattern with no recovery (`ollama_cpu_optional`, `soak_live_session_skip`, `soak_valid_reply`).
- Duplicate signature already open or queued in the same tick.
- Smoke/E2E LLM failure when live detector already flagged LLM this tick.

Implementation: `should_suppress_incident()`, `should_open_incident()`, `dedupe_detection_candidates()` in `incident_filters.py`.

## 2. Classify — before fixing

Order of precedence:
1. **Agent remediation pattern** — if matched, use its recovery plan (runs **before** code bug analysis in debug).
2. **Debug report** — operational vs configuration vs logic_bug.
3. **Code bug analyzer** — if code bug, **escalate immediately** (skip recovery chain).

Agent patterns **override** stale `code_bug` flags on soak false positives.

## 3. Fix — recovery boundaries

- Only **whitelisted** actions from `recovery.py`.
- **Skip all fixes** while live voice session active (`INTELLIGENT_SKIP_FIX_DURING_VOICE`).
- Max **tier** for auto-fix: `INTELLIGENT_MAX_AUTO_FIX_TIER` / `INTELLIGENT_AUTONOMOUS_MAX_TIER`.
- Max **attempts per hour** per signature: `INTELLIGENT_MAX_FIX_ATTEMPTS`.
- **Cooldown** between attempts: `INTELLIGENT_FIX_COOLDOWN_SECONDS`.

If pattern has **empty `recovery_actions`** → do not run fix chain; verify and auto-resolve only.

## 4. Verify — before resolved

- Post-fix: subsystem health + smoke (+ live Ollama probe if brain-related).
- Auto-resolve (no fixes): if benign pattern → **pattern validation only**.
- Failed verification → stay open or escalate; never mark resolved.

## 5. Report — when to email

| Event | Channel |
|-------|---------|
| Escalated / code bug | Immediate |
| Critical open | Immediate |
| Verified resolve after real recovery | Digest (or immediate if `INTELLIGENT_EMAIL_MODE=immediate`) |
| Benign auto-resolve (`ollama_cpu_optional`, soak skip, valid reply) | **Silent** — ops UI only |
| All-resolved benign digest batch | Summary: “handled automatically — no action needed” |

Implementation: `should_email_incident()` in `incident_filters.py`.

## 6. Learn

When `INTELLIGENT_LEARN_FROM_VERIFICATION=1`:
- Record verified fix outcomes in fix history + experience playbook.
- Reorder recovery chains by success rate.

## Adding a new agent pattern

1. Add pattern to `agent_remediation.py` → `classify_agent_remediation()`.
2. If benign/noise → add pattern id to `_SILENT_EMAIL_PATTERN_IDS` in `incident_filters.py`.
3. Add unit test in `test_intelligent_agent_remediation.py` and `test_intelligent_incident_filters.py`.
4. Document in this file and `AGENT_SYSTEM.md`.
