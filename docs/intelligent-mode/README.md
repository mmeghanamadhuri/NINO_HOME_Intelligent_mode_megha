# NiNO Intelligent Mode — Agent System Hub

This folder is the **single source of truth** for how NiNO’s agentic ops layer works: roles, job rules, files, and environment.

## Start here

| Document | Purpose |
|----------|---------|
| [AGENT_SYSTEM.md](./AGENT_SYSTEM.md) | Architecture, agent roster, pipeline, boundaries |
| [JOB_RULES.md](./JOB_RULES.md) | Enforceable rules (detect → classify → fix → verify → report) |
| [WORK_ENVIRONMENT.md](./WORK_ENVIRONMENT.md) | Data files, env vars, ops dashboard, daily checklist |
| [../INTELLIGENT_MODE_CHANGELOG.md](../INTELLIGENT_MODE_CHANGELOG.md) | Session-by-session code changes |

## Code map

```
server/intelligent_mode/
├── orchestrator.py      # Master conductor (45s tick)
├── detectors.py         # Live anomaly detection
├── smoke_tests.py       # L1 smoke (~45s)
├── e2e_voice_test.py    # L2 E2E voice
├── soak_test.py         # L3 soak (~90s background)
├── agent_remediation.py # Known false-alarm patterns
├── incident_filters.py  # Job rules: suppress + silent email
├── recovery.py          # Whitelisted recovery actions
├── workers.py           # Subsystem workers
├── verification_agent.py
├── debugger.py
├── code_bug_analyzer.py
├── digest.py + reporter.py
└── config.py
```

## Quick enable

```bash
# server/.env
INTELLIGENT_MODE=1
INTELLIGENT_AUTONOMOUS_RECOVERY=1
SOAK_TEST_ENABLED=1
INTELLIGENT_EMAIL_TO=ops@yourcompany.com
# ... SMTP settings — see WORK_ENVIRONMENT.md
```

Ops dashboard: **`http://<server>:5000/ops`**

## Principles

1. **Testing Agent finds problems** — smoke, E2E, soak, live monitoring.
2. **Recovery Agents fix ops issues only** — never edit source or flash firmware.
3. **Verification Agent confirms** before “resolved”.
4. **Code Bug Agent analyzes** — escalates to developer with suggested fix.
5. **Email Agent notifies humans** — immediate for critical/escalated; digest for the rest; **silent** for known benign patterns.
