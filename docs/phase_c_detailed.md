# Phase C Detailed Guide (Daily Summary Memory)

This document explains Phase C in detail: what `MEMORY_SUMMARY_CRON` controls, how daily summaries are generated and read back, and how that fits into the current NiNO server architecture.

---

## 1) What Phase C Is

Phase C is the **daily summary layer** for NiNO memory.

While Phase A stores every conversation turn and Phase B extracts durable facts, Phase C rolls up **an entire calendar day** of chat into a short, spoken-friendly summary stored in the `summaries` table.

In short:

- **Phase A** = recent turns from `conversations` (same session / same day continuity)
- **Phase B** = long-term facts in `memories` (preferences, stable details)
- **Phase C** = compressed day-level recap in `summaries` (cross-session context without loading hundreds of turns)

Phase C answers questions like: *“What did we talk about yesterday?”* without re-reading every raw turn from PostgreSQL.

---

## 2) What `MEMORY_SUMMARY_CRON=0` Means

`MEMORY_SUMMARY_CRON` is the **feature flag for Phase C**.

| Value | Meaning |
|-------|---------|
| `0` (default) | Phase C **disabled** — no summary generation on startup, `summaries` table stays empty unless you insert rows manually |
| `1` | Phase C **enabled** — on server startup, a background thread summarizes **yesterday’s** conversations per user |

Despite the name “CRON”, the current implementation is **not** a midnight scheduler. It is a **startup catch-up job**:

- Runs once when `memory_service.startup()` completes and the flag is on
- Targets **yesterday’s date only** (`date.today() - 1 day`)
- Does **not** re-run while the server stays up overnight

So `MEMORY_SUMMARY_CRON=0` simply means: *“Do not generate or maintain daily summaries.”* Phase A and Phase B continue to work normally.

---

## 3) Main Goal

Create compact, day-level memory so NiNO can:

- Recall prior sessions when recent `conversations` rows have aged out of the prompt window
- Give the LLM a short “what happened last time” block instead of a long transcript
- Support natural follow-ups across days (e.g. *“We were talking about Mars yesterday — want to continue?”*)

Summaries complement Phase B: facts like *“Favorite planet is Mars”* live in `memories`; summaries capture **topics and flow** from a whole day.

---

## 4) Prerequisites

Phase C can run only when:

1. **PostgreSQL memory is ready** — valid `DATABASE_URL`, schema applied, `memory_ready: true` in logs
2. **Phase A is logging conversations** — there must be rows in `conversations` for the target date
3. **`MEMORY_SUMMARY_CRON=1`** — set **before** server start (read in `configure_from_environ()`)
4. **Ollama reachable** — summary generation uses a second LLM call (same stack as Phase B extraction)

Phase C does **not** require Phase B (`MEMORY_EXTRACTION`), but enabling both is recommended for the richest cross-day context.

---

## 5) Enable Phase C

```bash
export DATABASE_URL="postgresql://nino:nino@127.0.0.1:5432/nino_memory"
export MEMORY_SUMMARY_CRON=1
```

Restart the server, then confirm:

```bash
curl -s http://127.0.0.1:8000/api/status | jq .memory
```

Expected fields:

```json
{
  "enabled": true,
  "ready": true,
  "summary_cron_enabled": true,
  "table_counts": {
    "users": 1,
    "conversations": 42,
    "memories": 5,
    "summaries": 0
  }
}
```

`summaries: 0` right after enable is normal until:

- You have conversations logged for **yesterday**, and
- You restart the server (catch-up runs on startup)

---

## 6) Module Wiring (Current Architecture)

| Module | Role in Phase C |
|--------|-----------------|
| `server/app.py` | Calls `get_memory_service().startup()` on boot; exposes `summary_cron_enabled` in `GET /api/status` |
| `server/memory_service.py` | **Write:** `_run_summary_catchup_safe()` → `_summarize_user_day()` · **Read:** `_fetch_latest_summary()` in `load_context()` |
| `server/voice_service.py` | Calls `load_context()` before LLM reply; passes `memory_ctx.prompt_block` (includes summary when present) |
| `server/llm_service.py` | `answer_voice_query()` prepends the memory block; `ollama_generate()` used for summary text |
| `server/scripts/memory_schema.sql` | Defines `summaries` table + unique `(user_id, summary_date)` |

Environment is read in `memory_service.configure_from_environ()`:

```python
SETTINGS.summary_cron_enabled = os.environ.get("MEMORY_SUMMARY_CRON", "0") in {"1", "true", "yes", "on"}
```

On startup, when enabled:

```python
threading.Thread(
    target=self._run_summary_catchup_safe,
    daemon=True,
    name="memory-summary-catchup",
).start()
```

---

## 7) Database Schema

```sql
CREATE TABLE summaries (
    id BIGSERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    summary_date DATE NOT NULL,
    summary_text TEXT NOT NULL,
    UNIQUE (user_id, summary_date)
);

CREATE INDEX idx_summaries_user_date ON summaries (user_id, summary_date DESC);
```

**One row per user per calendar day.** Re-running catch-up for the same day is a no-op (`ON CONFLICT DO NOTHING`).

---

## 8) Write Path — How Summaries Are Generated

### Trigger

Server startup → memory layer ready → background thread `memory-summary-catchup`.

### Algorithm (`_run_summary_catchup_safe`)

```mermaid
flowchart TD
    A[Server startup] --> B{MEMORY_SUMMARY_CRON=1?}
    B -->|no| Z[Skip Phase C]
    B -->|yes| C[Background thread starts]
    C --> D[target_date = yesterday]
    D --> E[SELECT DISTINCT user_id FROM conversations WHERE date = target_date]
    E --> F{For each user_id}
    F --> G{Summary row exists for user + date?}
    G -->|yes| F
    G -->|no| H[Load all conversations for that day]
    H --> I{Any rows?}
    I -->|no| F
    I -->|yes| J[Ollama: summarize transcript]
    J --> K[INSERT INTO summaries]
    K --> F
```

### Per-user summarization (`_summarize_user_day`)

1. **Skip if exists** — `SELECT 1 FROM summaries WHERE user_id = ? AND summary_date = ?`
2. **Load transcript** — all `conversations` for that user on that date, ordered by time
3. **Build prompt:**

   ```text
   Summarize this user's conversations on 2026-06-25 in 2-4 short bullet topics.
   Plain text only, suitable to read aloud tomorrow.

   User: My favorite planet is Mars.
   Assistant: Mars is the fourth planet from the Sun...
   User: What color is it?
   Assistant: Mars appears red because...
   ```

4. **Call Ollama** — `ollama_generate()`, `num_predict=128`, `timeout_s=60`
5. **Store result:**

   ```sql
   INSERT INTO summaries (user_id, summary_date, summary_text)
   VALUES ($1, $2, $3)
   ON CONFLICT (user_id, summary_date) DO NOTHING
   ```

### Example stored row

| user_id | summary_date | summary_text |
|---------|--------------|--------------|
| 1 | 2026-06-25 | - Discussed Mars and its red color\n- Talked about astronomy and planets\n- Asked follow-up questions about Mars |

### Important write-path behaviors

| Behavior | Detail |
|----------|--------|
| **Non-blocking** | Runs in a daemon thread; does not delay server boot or voice responses |
| **Yesterday only** | Each startup run processes one calendar day: yesterday |
| **Idempotent** | Existing summary for that user/day is never overwritten |
| **User-scoped** | Each recognized face maps to a `users` row; summaries are per user |
| **No conversation filter** | All turns logged that day are included (command paths that skip Phase A logging are excluded automatically) |

### Operational note

If the server runs continuously across midnight, **today’s conversations are not summarized until the next restart** (when “yesterday” includes that day). There is no in-process nightly timer yet.

---

## 9) Read Path — How Summaries Reach the LLM

Every voice turn with a recognized face calls `load_context()` before the main reply.

### Sequence

```mermaid
sequenceDiagram
    participant ESP as ESP32 (mic)
    participant VS as voice_service.py
    participant MS as memory_service.py
    participant PG as PostgreSQL
    participant LLM as Ollama (Qwen)

    ESP->>VS: WAV bytes
    VS->>VS: STT → user_text
    VS->>VS: _live_memory_viewer_name() → "Chakri"
    VS->>MS: load_context("Chakri", query_text=user_text)
    MS->>PG: UPSERT users
    MS->>PG: SELECT recent conversations (Phase A)
    MS->>PG: SELECT top memories (Phase B)
    MS->>PG: SELECT latest summary (Phase C)
    PG-->>MS: turns + facts + summary_text
    MS->>MS: _format_prompt_block()
    MS-->>VS: LoadedMemoryContext (prompt_block, has_summary=true)
    VS->>LLM: answer_voice_query(..., memory_context=prompt_block)
    LLM-->>VS: spoken reply
    VS-->>ESP: TTS WAV
```

### What gets loaded

`_fetch_latest_summary()` returns **one** row — the most recent `summary_date` for that user:

```sql
SELECT summary_text FROM summaries
WHERE user_id = $1
ORDER BY summary_date DESC
LIMIT 1
```

Older summaries remain in the database but are not stacked into the prompt; only the latest rollup is injected.

### Prompt block shape (`_format_prompt_block`)

When Phase C data exists, the voice LLM receives:

```text
You are speaking directly to Chakri. Always use second person (you/we). Never refer to Chakri in third person.

Known facts about them:
- Favorite planet is Mars

Earlier session summary:
- Discussed Mars and its red color
- Talked about astronomy and SpaceX launches

Recent things they asked or said (speech-to-text may have errors):
- User said: Tell me about Mars.
- User said: What color is it?

Known facts about them are authoritative — ...
```

**Order in the block:** identity line → Phase B facts → **Phase C summary** → Phase A recent user lines → usage rules.

### Latency impact

| Step | Typical cost |
|------|----------------|
| DB read (`summaries` + joins) | ~5–15 ms (indexed) |
| Extra LLM prefill tokens | ~+100–300 ms depending on summary length |

Summary generation itself (write path) is **never on the user’s critical path**.

---

## 10) Relationship to Other Memory Layers

```mermaid
flowchart LR
    subgraph Write["Write paths (async / background)"]
        A1[Voice turn logged] --> A2[conversations]
        A1 --> B1[Phase B extraction] --> B2[memories]
        A3[Startup catch-up] --> C1[Phase C summarize] --> C2[summaries]
    end

    subgraph Read["Read path (before each voice reply)"]
        R1[load_context] --> R2[Recent turns]
        R1 --> R3[Top memories]
        R1 --> R4[Latest summary]
        R2 & R3 & R4 --> R5[prompt_block → Qwen]
    end
```

| Layer | Table | Granularity | Best for |
|-------|-------|-------------|----------|
| Phase A | `conversations` | Per turn | “What did we just say?” / same-session follow-ups |
| Phase B | `memories` | Per fact | “What’s my favorite planet?” across weeks |
| Phase C | `summaries` | Per day | “What did we discuss yesterday?” / topic continuity |

Phase C does **not** replace Phase B. A summary might say *“talked about Mars”*; Phase B holds *“Favorite planet is Mars”* with an importance score.

---

## 11) Face Greeting vs Voice Context (Current vs Planned)

**Implemented today:** Phase C summary is injected only into the **voice query** path via `load_context()` → `answer_voice_query()`.

**Not yet wired:** Camera face greetings (`tts_service.py` → `greeting_for_face()`) do **not** receive the daily summary. Greetings use only the display name and whether the person was already seen this session (`is_return_visitor`).

Design docs describe cross-day greetings like *“Welcome back — yesterday we discussed Mars”*; that behavior would require passing `summary_text` (or the full prompt block) into `greeting_for_face()`. Until then, cross-day recall happens when the **user speaks**, not when the camera first recognizes them.

---

## 12) What Is Skipped (No Summary Generated)

| Condition | Result |
|-----------|--------|
| `MEMORY_SUMMARY_CRON=0` | Catch-up thread never starts |
| No `DATABASE_URL` / memory not ready | Phase C inactive |
| No conversations on target date for a user | That user skipped |
| Summary already exists for `(user_id, summary_date)` | Skipped (idempotent) |
| Ollama unreachable / timeout | Logged warning; no row inserted for that user |
| Server not restarted after a chat day | That day not summarized until a later startup includes it as “yesterday” |

---

## 13) Validation Checklist

1. `GET /api/status` → `memory.summary_cron_enabled: true`
2. Boot log: `Memory layer ready (PostgreSQL)` (no error from `Daily summary catchup failed`)
3. Ensure conversations exist for **yesterday**:

   ```sql
   SELECT user_id, COUNT(*) FROM conversations
   WHERE timestamp::date = CURRENT_DATE - 1
   GROUP BY user_id;
   ```

4. Restart server with `MEMORY_SUMMARY_CRON=1`
5. Check summaries:

   ```sql
   SELECT u.name, s.summary_date, LEFT(s.summary_text, 80)
   FROM summaries s JOIN users u ON u.id = s.user_id
   ORDER BY s.summary_date DESC;
   ```

6. `GET /api/memory/stats` → `table_counts.summaries` > 0
7. Voice session with recognized face → ask something that benefits from prior-day context; inspect server logs or prompt if debugging

---

## 14) Why You May See No Rows in `summaries`

- Flag left at default `MEMORY_SUMMARY_CRON=0`
- Server not restarted after enabling the flag
- All test conversations happened **today** — catch-up only processes **yesterday**
- User had no logged conversations on the target date (commands/recap/no-face paths do not log)
- Summary already created on a previous startup
- Ollama failure during generation (check logs for `Daily summary catchup failed`)

---

## 15) Feature Flags Reference

| Variable | Default | Phase C role |
|----------|---------|--------------|
| `DATABASE_URL` | (empty) | Required — enables PostgreSQL memory |
| `MEMORY_SUMMARY_CRON` | `0` | Set `1` to enable startup catch-up + summary reads |
| `OLLAMA_URL` | GPU `:11435` | Summary generation endpoint |
| `OLLAMA_MODEL` | `qwen2.5:1.5b` | Model for summarization |

Related (not Phase C, but loaded in the same prompt):

| Variable | Default | Role |
|----------|---------|------|
| `MEMORY_RECENT_TURNS` | `10` | Phase A turns in prompt |
| `MEMORY_EXTRACTION` | on when DB set | Phase B write path |
| `MEMORY_TOP_MEMORIES` | `10` | Phase B facts in prompt |

---

## 16) End-to-End Example (Two Days)

### Day 1 — Monday

User **Chakri** (recognized face) has several logged voice turns about Mars, astronomy, and SpaceX. Phase A writes to `conversations`; Phase B may extract facts into `memories`.

### Night / Tuesday startup

Server starts with `MEMORY_SUMMARY_CRON=1`:

1. Catch-up targets Monday’s date
2. Finds `user_id=1` had conversations Monday
3. Ollama produces a 2–4 bullet summary
4. Row inserted into `summaries` for `(user_id=1, summary_date=Monday)`

### Day 2 — Tuesday

Chakri returns. Recent Monday turns may have fallen out of the `MEMORY_RECENT_TURNS` window, but:

1. `load_context("Chakri")` loads Monday’s summary as **Earlier session summary**
2. Phase B may still load *“Favorite planet is Mars”*
3. User asks: *“Can we pick up where we left off?”*
4. Qwen uses the summary block to answer naturally about Mars / astronomy topics

---

## 17) Phase C Status in This Project

**Implemented and wired** — schema, startup catch-up, read path in `load_context()`, and prompt injection all exist in `memory_service.py`.

**Default deployment:** Phase C is **off** (`MEMORY_SUMMARY_CRON=0`) until explicitly enabled.

**Known limitations vs design docs:**

- Startup catch-up only (no true cron / midnight job while server runs)
- Only **yesterday** processed per startup (not full backfill of all historical days)
- Only **latest** summary loaded into prompts (not a rolling multi-day window)
- Face greetings do not yet use summary text

**Future (Phase D and beyond):** Semantic search (`pgvector`) may retrieve related memories by meaning; Phase C summaries remain useful as a cheap, human-readable day rollup alongside vector recall.

---

## 18) Quick “What Should I Expect?” Matrix

| Configuration | `summaries` table |
|---------------|-------------------|
| `DATABASE_URL` only | Empty (Phase C off) |
| `DATABASE_URL` + `MEMORY_SUMMARY_CRON=1` + conversations yesterday + restart | Rows appear after catch-up |
| All phases enabled | Populated based on usage; one summary max per user per day |

For Phase A/B behavior, see [`phase_b_detailed.md`](phase_b_detailed.md) and [`context_phase.md`](context_phase.md).
