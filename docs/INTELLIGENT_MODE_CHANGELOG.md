# Intelligent Mode — Change Log

This file records **what we changed and why** for NiNO Intelligent Mode, soak testing, and related ops email work.

**Maintenance rule:** Update this file at the end of **every prompt/session** — code changes, restarts, investigations, and decisions. Add a dated entry with: prompt, what we did, why, files touched (if any), and how to verify.

---

## 2026-08-23 — Session: Intelligent Mode agent-handled fixes + clearer emails

### Prompt
User asked that WAV-too-large, STT-empty, and unexpected-LLM-reply soak failures be handled by Intelligent Mode (not escalated to developers). Also asked for clearer Intelligent Mode emails, including plain-English sections for those three cases. User then asked to maintain this changelog file going forward.

### Why
- Soak tests were failing on long TTS replies, empty STT on the physical bot, and strict keyword matching on valid LLM answers.
- Those were labeled **“code bug — developer fix required”** and Intelligent Mode **skipped** auto-fix.
- Ops emails were technical and confusing; non-developers could not tell what action was needed.

### What we changed

#### 1. WAV auto-split (production fix)
| Item | Detail |
|------|--------|
| **Problem** | TTS replies exceeded ESP32 `/play_wav` limit (~380 KB) → soak failed with “WAV too large”. |
| **Fix** | `esp_playback.py` splits oversized WAV into frame-aligned clips and POSTs them sequentially. |
| **Why** | Fixes root cause at delivery layer so long answers (alarms, fun facts, stories) play without failure. |
| **Files** | `server/esp_playback.py`, `server/test_playback_busy.py` |

#### 2. Agent remediation (no developer escalation for known patterns)
| Item | Detail |
|------|--------|
| **Problem** | WAV / STT / unexpected-reply incidents were treated as code bugs. |
| **Fix** | New `agent_remediation.py` classifies these as **agent-handled** with recovery plans. |
| **Why** | Intelligent Mode should fix runtime/test issues itself; developer emails only for true code bugs. |
| **Files** | `server/intelligent_mode/agent_remediation.py` (new) |

**Agent-handled patterns:**

| Pattern ID | Trigger | Agent action |
|------------|---------|--------------|
| `wav_auto_split` | WAV too large | Auto-split (code) + `voice_pipeline_recovery` if needed |
| `voice_stt_recovery` | STT empty / no speech | `voice_pipeline_recovery` → whisper GPU/CPU reload → `voice_state_reset` |
| `soak_valid_reply` | Soak “unexpected reply” but reply is valid | Auto-resolve, no fix chain |
| `soak_reply_recovery` | Wording differed, may need retry | `voice_pipeline_recovery`, `voice_state_reset` |

**Orchestrator wiring:**
- `_agent_recovery_action()` picks preferred recovery steps first.
- `_maybe_fix()` auto-resolves immediately when no recovery steps needed (valid soak reply).
- Stale incidents auto-clear via `auto_resolve_reason()` from agent remediation.
- Code bugs still escalate; agent-handled incidents **do not** call `is_code_bug_incident()`.

**Files:** `server/intelligent_mode/orchestrator.py`, `server/intelligent_mode/code_bug_analyzer.py`, `server/intelligent_mode/debugger.py`

#### 3. Soak validation aligned with agent logic
| Item | Detail |
|------|--------|
| **Problem** | Soak failed on valid LLM replies before Intelligent Mode could classify them. |
| **Fix** | `_validate_voice_reply()` also calls `soak_reply_would_pass()` for keyword fallback. |
| **Why** | Fewer false failures at source; matches agent “valid reply” rules. |
| **Files** | `server/intelligent_mode/soak_test.py` |

#### 4. Clearer Intelligent Mode emails
| Item | Detail |
|------|--------|
| **Problem** | Emails said “code fix required” for issues the agent now handles; layout was hard to read. |
| **Fix** | Reporter overhaul: **IN PLAIN ENGLISH** block at top, **HANDLED BY INTELLIGENT MODE** section, subject lines `Auto-fixed` / `Auto-fix in progress`, lists all fix attempts. |
| **Why** | Recipients see what happened, what the agent did, and “no action needed” vs “developer needed” immediately. |
| **Files** | `server/intelligent_mode/reporter.py`, `server/test_intelligent_reporter.py` |

#### 5. Code bug handling (earlier in same session)
| Item | Detail |
|------|--------|
| **Problem** | Code bugs were falsely marked **Resolved** after ops fixes like `voice_pipeline_recovery`. |
| **Fix** | True code bugs → **escalated**, skip ops auto-fix, no false resolve; emails show developer fix required. |
| **Why** | Accurate status; ops restarts must not hide software bugs. |
| **Files** | `server/intelligent_mode/orchestrator.py`, `server/intelligent_mode/code_bug_analyzer.py`, `server/intelligent_mode/reporter.py` |

#### 6. Phase 1–3 Intelligent Mode improvements (earlier in same session)
| Phase | What | Why | Files |
|-------|------|-----|-------|
| **1** | Fix history — reorder recovery chains by past success rates | Try what worked before first | `fix_history.py`, `recovery.py` |
| **2** | LLM fix selection — pick next whitelisted action with confidence gate | Smarter recovery without unsafe commands | `llm_fix_selector.py`, `orchestrator.py` |
| **3** | Baseline anomalies — 3σ on rolling metrics | Catch slow regressions, not just hard failures | `baselines.py`, `detectors.py` |
| **Config** | `.env` / `.env.example` flags enabled | Turn on adaptive recovery on test server | `server/.env`, `server/.env.example` |

#### 7. Soak test environment
| Item | Detail |
|------|--------|
| **Problem** | `.env` line 88 broke bash `source` (SMTP FROM with `<>`). |
| **Fix** | Quoted `INTELLIGENT_SMTP_FROM='NiNO Automated Recovery <...>'`. |
| **Ops** | Restarted server + continuous soak via `run_soak_test_env.sh`. |
| **Result** | 33+ soak cycles; latest cycles green after WAV split (26/26 passed). |
| **Files** | `server/.env`, `server/scripts/run_soak_test_env.sh` |

### Tests added/updated
| File | Covers |
|------|--------|
| `server/test_intelligent_agent_remediation.py` | Agent pattern classification (new) |
| `server/test_intelligent_code_bug.py` | WAV/STT no longer code bugs; true bugs still email |
| `server/test_intelligent_mode_agents.py` | WAV runs agent fix; true code bug still escalates |
| `server/test_intelligent_reporter.py` | Plain English + agent-handled email sections |
| `server/test_playback_busy.py` | WAV split for ESP |
| `server/test_intelligent_fix_history.py` | Phase 1 |
| `server/test_intelligent_llm_fix_selector.py` | Phase 2 |
| `server/test_intelligent_baselines.py` | Phase 3 |

### How to verify
```bash
# Unit tests
cd server && python -m unittest test_intelligent_agent_remediation test_intelligent_reporter test_intelligent_code_bug test_playback_busy -q

# Live soak status
curl -s http://127.0.0.1:8000/api/intelligent-mode/soak/status | python3 -m json.tool

# Intelligent Mode status
curl -s http://127.0.0.1:8000/api/intelligent-mode/status | python3 -m json.tool
```

### Restart required
Server must be restarted after code changes for live soak and emails to use new behavior:
```bash
bash server/scripts/stop_soak_test_env.sh
bash server/scripts/run_soak_test_env.sh
```

---

## 2026-08-23 — Ops dashboard UI redesign (tabbed action center)

### Prompt
User wanted the Ops UI to be clearer: no scrolling to find sections, content should open/expand, auto-update showing agent-resolved vs developer-needed issues, show which agent is working, and scale cleanly when many bots are connected.

### Why
- The old single-page layout required scrolling to find incidents, bots, and tests.
- Stat cards scrolled to sections, which was confusing.
- With multiple bots, a long scrollable page becomes hard to operate.
- Agent-handled vs developer-needed issues were not visually separated.

### What we changed

#### 1. Tabbed navigation (no scroll-to-section)
| Tab | Content |
|-----|---------|
| **Overview** | 3-column action center + live agent strip |
| **Bot fleet** | Compact expandable bot cards |
| **Tests** | Soak + smoke |
| **Developer** | True code bugs only + all active incidents |
| **System** | Server health + firmware OTA |

Stat cards now **switch tabs** and highlight the relevant queue — no `scrollIntoView`.

#### 2. Issue queues API (`issue_queues`)
| Queue | Meaning |
|-------|---------|
| `agent_working` | Intelligent Mode is fixing now |
| `developer` | True software/firmware bug — human needed |
| `agent_resolved` | Recently auto-fixed by agent |

Dashboard summary adds: `agent_handling_incidents`, `agent_resolved_recent`, `developer_needed_incidents`.

#### 3. Live agent activity
- Pulsing “Live now” strip shows which agent is working on which bot and current action.
- Queue items expand with plain-English explanation, fix attempts, and status badges.

#### 4. Compact bot cards
- Each bot is a collapsible `<details>` card — health + agent status visible at a glance; expand for subsystems, incidents, OTA.

### Files
| File | Change |
|------|--------|
| `server/templates/ops.html` | Tab layout, action center, stat cards |
| `server/static/ops.js` | Tab switching, queue rendering, auto-refresh (8s), no scroll |
| `server/static/ops.css` | Tabs, queues, live agent pulse, compact bots |
| `server/intelligent_mode/dashboard.py` | `issue_queues`, enhanced `agent_activity`, summary counts |
| `server/intelligent_mode/incident_ui.py` | `queue` field: agent_working / developer / agent_resolved |
| `server/test_intelligent_dashboard.py` | Asserts `issue_queues` present |

### How to verify
```bash
cd server && python -m unittest test_intelligent_dashboard -q

# Open in browser (hard refresh to bust cache)
# http://<server>:8000/ops
# ops.css?v=4  ops.js?v=6
```

### Restart required
Restart server so dashboard API returns `issue_queues` and new UI loads:
```bash
bash server/scripts/stop_soak_test_env.sh
bash server/scripts/run_soak_test_env.sh
```

### 2026-08-23 follow-up — stat cards no longer scroll the page
| Item | Detail |
|------|--------|
| **Problem** | Clicking stat cards scrolled/walked the page down to detail sections. |
| **Fix** | Cards now open an **inline detail panel** directly below the stat row (no scroll, no tab jump). Click again or Close to dismiss. |
| **Files** | `ops.html`, `ops.js?v=7`, `ops.css?v=5` |

---

## 2026-08-23 — Verification agent cross-checks fixes before "Resolved"

### Prompt
User reported incidents marked "Voice assistant restored / Resolved" while Ollama was still down (11434 connection errors). Wanted a cross-check agent to confirm fixes are real, not just labeled resolved.

### Why
- Incidents were auto-resolved when the health check stopped re-detecting them — **without** live verification.
- Bot voice incidents ignored failing **server** Ollama smoke tests.
- Emails said "restored" even when `/api/generate` still failed.

### What we changed

#### 1. Verification agent (`verification_agent.py`)
Before any incident is marked **resolved**, checks:
| Check | What it does |
|-------|----------------|
| `subsystem_health` | Existing per-subsystem health probe |
| `llm_snapshot` | LLM reachable in live snapshot |
| `ollama_live_generate` | Real `/api/generate` probe with short prompt |
| `smoke_tests` | Smoke suite (server + bot for voice incidents) |
| `auto_resolve_valid` | Only for auto-resolve paths — pattern still applies |

#### 2. All resolve paths gated
- Post-fix `_verify_and_finalize`
- Stale soak auto-resolve
- "Health check no longer reports" auto-resolve
- Agent immediate auto-resolve

#### 3. Smoke fix for bot voice incidents
`device_smoke_passed()` now requires **server Ollama smoke** to pass for voice/llm/stt/tts incidents (was only checking bot tests).

#### 4. Emails
- Subject/headline: **"verified fixed"** only when verification passed
- No resolve email if verification failed

### Files
`verification_agent.py` (new), `orchestrator.py`, `workers.py`, `smoke_tests.py`, `incidents.py`, `config.py`, `reporter.py`, `test_intelligent_verification_agent.py`, `.env.example`

### How to verify
```bash
cd server && python -m unittest test_intelligent_verification_agent -q
# Stop GPU Ollama, trigger voice incident — should stay open, not "Resolved"
```

---

## 2026-08-23 — Session follow-ups (ops, restarts, tracking)

### Prompt
Multiple follow-up prompts: stat cards should not scroll the page; restart server + soak with all fixes; explain last soak failure; confirm problems are tracked; confirm auto-fixes are ready for repeat issues; ensure every prompt logs to MD.

### Why
User needs a running audit trail — not just code changes, but restarts, investigations, and decisions — so nothing is lost between chat sessions.

### What we did

#### 1. Stat cards — no scroll (follow-up to Ops UI)
| Item | Detail |
|------|--------|
| **Problem** | Clicking stat cards still scrolled/walked the page (old UI cached on server). |
| **Fix** | Inline detail panel below stat row; `preventDefault`; no tab jump; cache bust `ops.js?v=7`, `ops.css?v=5`. |
| **Files** | `ops.html`, `ops.js`, `ops.css` |

#### 2. Server + soak restarts
| When | Result |
|------|--------|
| Restart for Ops UI | Smoke 10/10, E2E 4/4, soak cycles 42–56 green |
| Restart for verification agent | Same stack; soak running |
| User-requested full restart | Smoke 10/10, E2E 4/4; cycles 58–60 green, 61–62 failed on soak keyword |

#### 3. Last soak failure investigation (cycle 26/27)
| Item | Detail |
|------|--------|
| **Failed test** | `what_is_2_plus_2` — reply `"Two!"` (expected `"4"` or `"four"`) |
| **Not** | Ollama down — GPU Ollama on 11435 was healthy |
| **Cause** | Soak keyword mismatch + possible live ESP `wake_reject` race during soak |
| **Action** | Intelligent Mode should treat as agent-handled false alarm if reply valid; wrong answer `"Two!"` may need soak keyword tuning |

#### 4. Problem tracking confirmed
| Layer | Location |
|-------|----------|
| Live incidents | `server/data/intelligent_incidents.json` (~240 incidents) |
| Fix history | `docs/INTELLIGENT_MODE_CHANGELOG.md` (this file) |
| Ops dashboard | `/ops` — queues, verification status |
| Soak | `/api/intelligent-mode/soak/status` |

#### 5. Auto-fix readiness confirmed
Repeat issues (WAV too large, STT empty, soak false alarms, Ollama restart) have agent remediation + verification agent gates. True code bugs still escalate to Developer tab.

#### 6. Logging rule (this prompt)
**User request:** Log what we did in MD on **every prompt**, not only code-changing sessions.  
**Current mechanism:** Manual update to this changelog by the agent (not an automated git hook).  
**Going forward:** Append an entry per prompt/session — code change, restart, investigation, or decision.

### How to verify
```bash
tail -80 docs/INTELLIGENT_MODE_CHANGELOG.md
curl -s http://192.168.1.156:8000/api/intelligent-mode/soak/status | python3 -m json.tool
```

---

## 2026-08-23 — Fix recurring false voice incidents (11434, soak skip, wake reject)

### Prompt
User asked to fix recurring failures: soak live-session skips counted as fails, `FixAttempt` NameError, and repeated “Voice assistant verified fixed” emails for Ollama port 11434 connection refused when GPU Ollama (11435) is healthy.

### Why
- **11434 errors** are CPU Ollama fallback noise — GPU on 11435 is the live primary; incidents opened anyway, then “verified fixed” spammed ops email.
- **Soak “skipped — live voice session active”** was counted as `failed`, turning scheduling conflicts into red soak cycles and voice incidents.
- **`workers.py` NameError** (`FixAttempt` not imported) crashed Intelligent Mode ticks during recovery.

### Root cause (why the same issues kept repeating)

| Symptom | Root cause |
|---------|------------|
| `port=11434` “verified fixed” loop | Latency log recorded CPU fallback errors; detector opened voice incidents; verification passed because GPU 11435 was fine |
| Soak cycle 23 red (24/27) | Live voice session blocked 3 tests but they were marked `failed`, not `skipped` |
| `AGENT ESCALATE fix_cap` noise | Same stale 11434 signature re-opened every poll until fix cap, then auto-verified |
| Intelligent mode tick crash | Missing `FixAttempt` / `Incident` imports in `workers.py` |

### What we changed

| Fix | Detail | Files |
|-----|--------|-------|
| **Voice incident filters** | New module suppresses CPU-only 11434 errors when GPU reachable; ignores `wake_reject` latency rows | `voice_incident_filters.py` (new) |
| **Detector filter** | `_recent_voice_failures()` drops benign latency rows before opening incidents | `detectors.py` |
| **Orchestrator skip** | `SKIP_DETECT` for suppressed voice errors (both tick + soak remediate paths) | `orchestrator.py` |
| **Agent patterns** | `ollama_cpu_optional` + `soak_live_session_skip` auto-resolve with no recovery chain | `agent_remediation.py` |
| **Soak skip ≠ fail** | Live-session defer returns `skipped=True`; cycle `ok` only counts real failures | `soak_test.py`, `smoke_tests.py` |
| **Workers crash** | Import `FixAttempt`, `Incident` in `workers.py` | `workers.py` |
| **Tests** | 8 new filter/soak tests | `test_intelligent_voice_incident_filters.py` |

### How to verify

```bash
cd server
python3 -m unittest test_intelligent_voice_incident_filters.py test_intelligent_soak.py -q

# After server reload — soak should stay green when voice session blocks tests:
curl -s http://192.168.1.156:8000/api/intelligent-mode/soak/status | python3 -m json.tool

# Logs should show SKIP_DETECT (not DETECT+VERIFIED) for 11434 when GPU up:
grep -E 'SKIP_DETECT|11434|Soak cycle' data/nino_server.log | tail -20
```

### Expected behavior going forward
- No new incidents/emails for **11434 connection refused** when GPU Ollama is healthy.
- Soak cycles stay **green** when voice tests are skipped due to live session.
- No more **`NameError: FixAttempt`** during Intelligent Mode recovery.

---

## 2026-08-23 — Ops dashboard tabs not switching panels

### Prompt
User reported Ops dashboard tabs (Overview, Bot fleet, Tests, Developer, System) highlight on click but no panel content appears to open.

### Why
- `.ops-tab-panel { display: grid }` could override the HTML `[hidden]` attribute in some cases (same specificity, later rule wins), leaving all panels visible stacked or none switching visibly.
- Tab click handlers used per-button `onclick` assignment without event delegation; a JS error during init could skip binding.
- No scroll/focus cue when switching tabs — panel changed below the fold looked like “nothing happened”.

### What we changed

| Fix | Detail | Files |
|-----|--------|-------|
| **Panel visibility CSS** | `[hidden] { display: none !important }` + `:not([hidden]) { display: grid }` | `static/ops.css?v=7` |
| **Tab switching JS** | Event delegation on `#opsTabs`, keyboard arrows, scroll active panel into view | `static/ops.js?v=8` |
| **Safe init** | `DOMContentLoaded` wrapper, optional chaining on toolbar buttons | `static/ops.js` |
| **A11y** | `aria-controls` / `aria-labelledby` on tabs and panels | `templates/ops.html` |

### How to verify
1. Hard refresh Ops page: `http://192.168.1.156:8000/ops` (Ctrl+Shift+R).
2. Click **Bot fleet** — bot cards should replace the Overview action center.
3. Click **Tests** — soak + smoke tables appear.
4. Click **System** — server brain + firmware OTA appear.

---

## 2026-08-23 — Experience learning loop (playbook + verification-weighted fixes)

### Prompt
User asked agents to **learn from session data** to improve performance — not just record incidents, but use verified outcomes to pick better recovery actions over time.

### Why
- Fix history and LLM selector existed but were **bypassed** when agent remediation matched first (static rule order).
- `fix.success` meant “worker returned OK”, not “incident verified fixed” — inflated success rates.
- Verification reports were stored but never fed back into action selection.

### What we changed

| Fix | Detail | Files |
|-----|--------|-------|
| **Experience playbook** | New JSON store `{error_pattern → action → verified_pass/total}`; patterns normalized (soak, 11434, WAV, STT, etc.) | `experience_playbook.py` (new), `data/intelligent_experience_playbook.json` |
| **Agent actions use history** | `preferred_recovery_actions()` reorders by playbook + verified fix-history | `agent_remediation.py` |
| **LLM not short-circuited** | LLM runs when agent plan has **multiple** actions or on retry after failed fix; gets agent chain as `allowed_actions` | `orchestrator.py`, `llm_fix_selector.py` |
| **Verification-weighted learning** | After verify pass/fail: record playbook, optionally set `fix.success = worker_ok AND verified` | `orchestrator.py`, `fix_history.py` |
| **Config flags** | `INTELLIGENT_EXPERIENCE_PLAYBOOK=1`, `INTELLIGENT_LEARN_FROM_VERIFICATION=1` (default on) | `config.py`, `.env.example` |
| **Tests** | Pattern normalization, playbook reorder, verified-only stats | `test_intelligent_experience_playbook.py` |

### How to verify
```bash
cd server
python3 -m unittest test_intelligent_experience_playbook.py test_intelligent_fix_history.py -q
# After a few verified incidents, check:
# data/intelligent_experience_playbook.json — patterns with verified_pass counts
# debug_report.fix_selection — should appear when agent plan has 2+ actions and LLM enabled
```

Restart soak env after deploy:
```bash
bash server/scripts/stop_soak_test_env.sh
bash server/scripts/run_soak_test_env.sh
```

---

```markdown
## YYYY-MM-DD — Short title

### Prompt
(one line: what the user asked)

### Why
(business/technical reason)

### What we changed
- **Problem:** …
- **Fix:** …
- **Files:** …

### How to verify
(commands or soak cycle check)
```

---

## Quick reference — files touched this session

| Area | Files |
|------|-------|
| ESP playback | `server/esp_playback.py` |
| Agent remediation | `server/intelligent_mode/agent_remediation.py` |
| Orchestrator | `server/intelligent_mode/orchestrator.py` |
| Code bug analyzer | `server/intelligent_mode/code_bug_analyzer.py` |
| Debugger | `server/intelligent_mode/debugger.py` |
| Reporter / email | `server/intelligent_mode/reporter.py` |
| Soak test | `server/intelligent_mode/soak_test.py` |
| Phase 1–3 | `fix_history.py`, `llm_fix_selector.py`, `baselines.py`, `recovery.py`, `detectors.py`, `config.py` |
| Config | `server/.env`, `server/.env.example` |
| Tests | `test_intelligent_*.py`, `test_playback_busy.py` |
| This log | `docs/INTELLIGENT_MODE_CHANGELOG.md` |
