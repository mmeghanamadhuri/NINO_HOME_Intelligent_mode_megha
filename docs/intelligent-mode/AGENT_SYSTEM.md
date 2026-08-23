# Intelligent Mode — Agent System

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    MASTER ORCHESTRATOR (~45s)                    │
│  collect → detect → debug → classify → fix → verify → email     │
└─────────────────────────────────────────────────────────────────┘
         │                    │                    │
         ▼                    ▼                    ▼
   TESTING AGENT        RECOVERY AGENTS      VERIFICATION AGENT
   L1 smoke             LLM / STT / TTS      health + smoke + live probe
   L2 E2E voice         Voice / Camera       auto-resolve validation
   L3 soak (90s loop)   Discovery / Memory
         │                    │
         ▼                    ▼
   DEBUG AGENT            CODE BUG AGENT
   root cause             developer escalation
         │
         ▼
   EMAIL DIGEST AGENT
   immediate vs batch vs silent
```

## Agent job cards

### Master Orchestrator
- **Runs:** Every `INTELLIGENT_POLL_SECONDS` (default 45s).
- **Must:** Grace-period debounce; skip fixes during live voice; escalate code bugs immediately.
- **Must not:** Patch code, flash firmware, or bypass verification.

### Testing Agent (L1 / L2 / L3)
| Layer | Interval | What it tests |
|-------|----------|---------------|
| L1 Smoke | Orchestrator tick | Ollama, Whisper, TTS, bot HTTP, camera, playback |
| L2 E2E | Orchestrator tick (skip if voice active) | Scripted LLM Q&A + in-process voice pipeline |
| L3 Soak | Separate 90s loop | Live ESP mic/speaker, memory, face, bot status |

- **Must:** Mark intentional skips as `skipped=True` — never open incidents for skips.
- **Must not:** Run soak live tests while user is in a voice session (defer instead).

### Anomaly Detector
- **Runs:** Each orchestrator tick on live snapshot + `latency_log.json`.
- **Must:** Respect grace (`INTELLIGENT_GRACE_SECONDS`, camera 120s).
- **Must not:** Open incidents for suppressed patterns (see `incident_filters.py`).

### Debug Agent
- **Runs:** Before fix decision when `INTELLIGENT_SELF_DEBUG=1`.
- **Output:** category, root cause, fixable_by_agent, suggested actions.
- **Must:** Refresh after each fix attempt.

### Agent Remediation (pattern matcher)
| Pattern | Meaning | Email? |
|---------|---------|--------|
| `soak_live_session_skip` | Soak deferred — user talking | Silent |
| `ollama_cpu_optional` | CPU :11434 down, GPU OK | Silent |
| `soak_valid_reply` | Valid reply, strict keywords | Silent |
| `wav_auto_split` | Long TTS auto-split | Digest |
| `voice_stt_recovery` | STT empty | Digest |
| `soak_reply_recovery` | Wording differed | Digest |

### Recovery Workers
- **Whitelist only** — see `workers.py` `ALLOWED_FIX_ACTIONS`.
- **Max chain steps:** `INTELLIGENT_RECOVERY_CHAIN_STEPS` (default 2) per tick.
- **Must not:** Modify `.py` source, `.env`, or OTA flash (unless explicit developer OTA from `/ops`).

### Verification Agent
- **Runs:** Before any incident is marked `resolved`.
- **Checks:** subsystem health, LLM snapshot, optional live Ollama probe, smoke.
- **Benign auto-resolve:** Only validates agent pattern — skips heavy probes.

### Code Bug Analyzer
- **Runs:** During debug for logic_bug / regression signals.
- **Must:** Block auto-fix; email developer if escalated (`INTELLIGENT_EMAIL_CODE_BUGS`).
- **Must not:** Apply code patches or auto-OTA (default off).

### Email / Reporter
- **Immediate:** critical open, escalated, code bugs.
- **Digest (15 min):** verified resolves with real recovery.
- **Silent:** benign patterns above — visible in `/ops` only.

## Boundaries (non-negotiable)

| Agent | Can do | Cannot do |
|-------|--------|-----------|
| Testing | Probe, synthetic audio, soak Q&A | Change config permanently |
| Recovery | Restart streams, warm Ollama, reload models | Edit source, flash firmware |
| Verification | Read health, run probes | Mark resolved without checks |
| Code bug | Analyze, suggest files/fix | Apply patches |
| Email | Notify humans | Block on unresolved critical without escalate |
