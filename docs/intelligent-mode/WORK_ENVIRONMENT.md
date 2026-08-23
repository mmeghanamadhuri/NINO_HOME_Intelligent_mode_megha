# Intelligent Mode — Work Environment

## Runtime layout

| Path | Role |
|------|------|
| `server/intelligent_mode/` | All agent code |
| `server/data/intelligent_incidents.json` | Incident store (persistent) |
| `server/data/intelligent_smoke_tests.json` | Smoke run history |
| `server/data/soak_test.json` | Soak cycle state |
| `server/data/intelligent_experience_playbook.json` | Learned fix ordering |
| `server/data/intelligent_baselines.json` | Baseline metrics |
| `server/data/latency_log.json` | Voice query latency (detector input) |
| `server/data/nino_server.log` | Server log |
| `server/.env` | Live configuration (never commit secrets) |
| `server/.env.example` | Documented defaults |

## Ops surfaces

| URL / tool | Use |
|------------|-----|
| `/ops` | Live dashboard — KPIs, bot fleet, tests, developer queue |
| `/api/intelligent-mode/status` | JSON status for scripts |
| `POST /api/intelligent-mode/reload` | **Apply code/config changes without full server restart** |
| `POST /api/intelligent-mode/incidents/prune` | Archive + remove resolved benign incidents |
| `server/scripts/run_intelligent_tests.sh` | Run unit tests |
| `server/scripts/send_intelligent_report.py` | Manual report trigger |

## Environment variables (essential)

```bash
# Core
INTELLIGENT_MODE=1
INTELLIGENT_POLL_SECONDS=45
INTELLIGENT_GRACE_SECONDS=90
INTELLIGENT_CAMERA_GRACE_SECONDS=120
INTELLIGENT_LLM_GRACE_SECONDS=120
INTELLIGENT_AUTONOMOUS_RECOVERY=1
INTELLIGENT_RECOVERY_CHAIN_STEPS=2

# Testing layers
INTELLIGENT_SMOKE_TESTS=1
INTELLIGENT_E2E_TESTS=1
SOAK_TEST_ENABLED=1
SOAK_LIVE_ESP=1                    # live ESP soak (requires bot)

# Voice safety
INTELLIGENT_SKIP_FIX_DURING_VOICE=1
INTELLIGENT_SKIP_E2E_DURING_VOICE=1

# Ollama
OLLAMA_GPU_URL=http://127.0.0.1:11435/api/generate
OLLAMA_HTTP_RETRIES=6
OLLAMA_HTTP_RETRY_DELAY_S=0.4

# Email
INTELLIGENT_EMAIL_TO=ops@example.com
INTELLIGENT_SMTP_HOST=smtp.example.com
INTELLIGENT_SMTP_USER=...
INTELLIGENT_SMTP_PASSWORD=...
INTELLIGENT_EMAIL_MODE=digest
INTELLIGENT_EMAIL_DIGEST_SECONDS=900
INTELLIGENT_EMAIL_ON_RESOLVE=1
INTELLIGENT_EMAIL_ON_ESCALATE=1
INTELLIGENT_EMAIL_CODE_BUGS=1

# Verification & learning
INTELLIGENT_VERIFICATION_LIVE_PROBES=1
INTELLIGENT_LEARN_FROM_VERIFICATION=1
INTELLIGENT_EXPERIENCE_PLAYBOOK=1
INTELLIGENT_PRUNE_ON_START=1
INTELLIGENT_INCIDENTS_KEEP_RESOLVED=50
```

Full list: `server/.env.example` and `server/intelligent_mode/config.py`.

## Daily ops checklist

1. Open `/ops` — confirm **Developer needed = 0** (or only real open items).
2. Check GPU Ollama: `curl -s http://127.0.0.1:11435/api/tags`.
3. If soak enabled — verify soak panel shows passes (not stuck on skips).
4. After code changes — restart server; run `bash server/scripts/run_intelligent_tests.sh`.
5. Review digest email — should be quiet unless real recovery or escalation.

## Developer workflow (agentic)

When editing Intelligent Mode:

1. Read `docs/intelligent-mode/JOB_RULES.md`.
2. Cursor rule `.cursor/rules/intelligent-mode.mdc` applies to `server/intelligent_mode/**`.
3. Add/update tests alongside behavior changes.
4. Never commit `.env` or incident JSON with secrets.
5. Log meaningful changes in `docs/INTELLIGENT_MODE_CHANGELOG.md`.

## Restart after changes

**Option A — hot reload (no full server restart):**

```bash
curl -X POST http://127.0.0.1:5000/api/intelligent-mode/reload
```

**Option B — prune old resolved noise only:**

```bash
curl -X POST "http://127.0.0.1:5000/api/intelligent-mode/incidents/prune?keep=50"
```

**Option C — full server restart** (if hot reload is not enough):

```bash
# From server/ — however you normally run NiNO
python app.py
```

Static ops UI changes (`ops.css`, `ops.js`) — hard refresh browser only.
