# NiNO Memory Context Phases

This file explains the PostgreSQL memory roadmap used by NiNO, what each phase is responsible for, and what is already active in the current project.

---

## Big Picture

NiNO memory is designed in progressive phases:

- **Phase A**: Conversation continuity (short-term memory)
- **Phase B**: Long-term fact extraction
- **Phase C**: Daily summaries across sessions
- **Phase D**: Semantic recall with vector search

The architecture is intentionally incremental so the assistant can be useful early (Phase A) while advanced recall features are enabled later.

---

## Current Database Tables

The schema includes these tables:

- `users`
- `conversations`
- `memories`
- `summaries`

Important: even though all four tables exist, data population depends on which phase is enabled.

---

## Phase A - Conversation Memory (Active Core)

### Goal

Enable same-session and same-day continuity:

- "What did we just discuss?"
- Follow-up replies with recent context

### What Phase A Does

1. Resolve a user identity from recognized face/display name.
2. Upsert the user in `users`.
3. Load recent turns from `conversations`.
4. Inject those recent turns into LLM prompt context.
5. Log new user-assistant exchanges back to `conversations` (background thread).

### Tables Used in Phase A

- `users` (active writes)
- `conversations` (active reads + writes)

### Runtime Behavior

- Runs when PostgreSQL is configured and memory service is ready.
- Logging is non-blocking (done in background).
- Non-conversational command paths may be skipped from memory logging.
- STT fragments are filtered to avoid low-quality memory context.

### Result

Phase A is the base memory layer and should be considered required for personalized continuity.

---

## Phase B - Long-Term Memory Extraction (Optional, Feature Flag)

### Goal

Store durable personal facts that remain useful across many conversations.

Examples:

- User preferences ("favorite planet is Mars")
- Repeated interests ("likes SpaceX launches")
- Stable personal details ("works in robotics")

Phase B builds on Phase A. It does **not** replace recent-turn recall — it adds a `memories` table that survives across sessions.

### Prerequisites

Phase B is already wired in code. You only need:

1. Phase A working (`DATABASE_URL` set, `memory_ready: true` in logs).
2. A recognized face at query time (same rule as Phase A).
3. `MEMORY_EXTRACTION=1` in the environment **before server start**.
4. Ollama reachable (extraction uses a second LLM call after each logged turn).

### Enable Phase B

```bash
export DATABASE_URL="postgresql://nino:nino@127.0.0.1:5432/nino_memory"
export MEMORY_EXTRACTION=1
export MEMORY_MIN_IMPORTANCE=5    # default — only store scores >= 5
export MEMORY_TOP_MEMORIES=10     # default — max facts loaded into prompt
```

Restart the server, then confirm:

```bash
curl -s http://127.0.0.1:8000/api/status | jq .memory
```

Expected:

```json
{
  "enabled": true,
  "ready": true,
  "extraction_enabled": true,
  "database_url_set": true
}
```

Leave `MEMORY_SUMMARY_CRON=0` until Phase B is validated (Phase C is separate).

---

### Module Wiring (how the code connects)

| Module | Role in Phase B |
|--------|-----------------|
| `server/app.py` | Calls `get_memory_service().startup()` on boot; exposes `memory` in `GET /api/status` |
| `server/voice_service.py` | Resolves live face → loads context → logs conversational replies |
| `server/memory_service.py` | **Read:** `_fetch_top_memories()` · **Write:** `_extract_memories_safe()` |
| `server/llm_service.py` | Injects `memory_context` prompt block into `answer_voice_query()` / identity replies |
| `server/scripts/memory_schema.sql` | Defines `memories` table |
| `server/scripts/init_memory_db.sh` | Creates DB + applies schema |

Environment is read in `memory_service.configure_from_environ()`:

- `MEMORY_EXTRACTION=1` → `SETTINGS.extraction_enabled = True`
- `MEMORY_MIN_IMPORTANCE` → write + read threshold
- `MEMORY_TOP_MEMORIES` → how many facts to load before each reply

---

### End-to-End Flow

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
    VS->>MS: load_context("Chakri")
    MS->>PG: UPSERT users, SELECT conversations, SELECT memories
    PG-->>MS: recent turns + stored facts
    MS-->>VS: prompt_block (includes Known facts)
    VS->>LLM: answer_voice_query(..., memory_context=prompt_block)
    LLM-->>VS: spoken reply
    VS->>MS: log_conversation_for_viewer() [background]
    MS->>PG: INSERT conversations
    Note over MS,LLM: Only if MEMORY_EXTRACTION=1
    MS->>LLM: extraction prompt (JSON memories)
    LLM-->>MS: [{memory, importance}, ...]
    MS->>PG: INSERT memories (importance >= 5)
    VS-->>ESP: TTS WAV (user hears reply immediately)
```

Phase B has two paths:

- **Read path (before reply):** load top memories → inject into main voice LLM prompt.
- **Write path (after reply):** background extraction → insert durable facts.

The user never waits for extraction; it runs after TTS starts.

---

### Step-by-step example session

Assume **Chakri** is recognized by the camera and Phase A + B are enabled.

#### Turn 1 — user shares a durable fact

| Step | What happens |
|------|----------------|
| 1 | User says: *"My favorite planet is Mars."* |
| 2 | `voice_service.process_voice_wav()` transcribes audio |
| 3 | `_live_memory_viewer_name()` returns `"Chakri"` (camera state = `recognized`) |
| 4 | `memory_svc.load_context("Chakri")` runs (~15–25 ms) |
| 5 | DB read: `users` row exists; `conversations` may be empty; `memories` empty on first run |
| 6 | `answer_voice_query(..., memory_context=...)` — reply generated with no prior facts yet |
| 7 | `reply_path = "llm"` → `log_conversation_for_viewer()` queues background INSERT |
| 8 | Background thread inserts into `conversations` |
| 9 | `after_conversation_logged()` starts extraction thread (Phase B) |
| 10 | Ollama extraction returns e.g. `[{"memory": "Favorite planet is Mars", "importance": 8}]` |
| 11 | Row inserted into `memories` (8 >= 5) |

**Latency log fields** (from `voice_service.py`):

```json
{
  "reply_path": "llm",
  "memory_viewer": "Chakri",
  "memory_ready": true,
  "memory_store": "queued",
  "memory_turns": 1,
  "memory_facts": 0
}
```

`memory_facts: 0` on turn 1 is normal — facts are extracted **after** the reply, so the count reflects what was loaded **before** this turn.

#### Turn 2 — same session, follow-up

User says: *"What color is it?"*

| Step | What happens |
|------|----------------|
| 1 | `load_context("Chakri")` loads recent turn + **1 memory fact** |
| 2 | Prompt block now includes both recent history and known facts |
| 3 | LLM can answer "red" using session context about Mars |
| 4 | New exchange logged; extractor may add another memory or skip low-value turn |

**Latency log:**

```json
{
  "memory_turns": 2,
  "memory_facts": 1
}
```

#### Turn 3 — new session, days later

User says: *"What's my favorite planet?"*

Even if `conversations` from last week are trimmed, `memories` still has:

```text
Favorite planet is Mars  (importance 8)
```

`load_context()` injects it under **Known facts about them** in the prompt, so NiNO can answer without the user repeating themselves.

---

### What gets injected into the LLM (read path)

`memory_service._format_prompt_block()` builds a single string passed as `memory_context`. Example after Phase B has data:

```text
You are speaking directly to Chakri. Always use second person (you/we). Never refer to Chakri in third person.

Known facts about them:
- Favorite planet is Mars
- Interested in SpaceX rocket launches

Recent session history (may contain speech-to-text errors — ignore fragments):
- User said: Tell me about Mars. | Assistant replied: Mars is the fourth planet from the Sun...
- User said: What color is it? | Assistant replied: Mars appears red because of iron oxide on its surface.

If they ask for context, summarize the discussion naturally...
```

`llm_service.answer_voice_query()` prepends this block:

```text
You are NiNO, a concise voice assistant...
You are speaking directly to Chakri...
Session history is provided below. Use it when relevant...
[memory_context block above]
Rules: one short spoken reply under 40 words...
The user asked: What's my favorite planet?
```

---

### What the extractor sends (write path)

After `INSERT INTO conversations`, `_extract_memories_safe()` calls Ollama with:

```text
Extract useful long-term memories from this conversation.
Return JSON only: a list of objects with keys memory (string) and importance (integer 0-10).
Only include importance >= 5.

User:
My favorite planet is Mars.

Assistant:
Mars is the fourth planet from the Sun. It has a reddish surface...
```

**Example valid response → stored rows:**

```json
[
  {"memory": "Favorite planet is Mars", "importance": 8}
]
```

**Example response → nothing stored:**

```json
[
  {"memory": "Asked about Mars right now", "importance": 2},
  {"memory": "Wants a short answer", "importance": 3}
]
```

Scores below `MEMORY_MIN_IMPORTANCE` (default 5) are dropped.

---

### Database example after a few sessions

**`users`**

| id | face_id | name | last_seen |
|----|---------|------|-----------|
| 1 | chakri | Chakri | 2026-06-25 14:30:00 |

**`conversations`** (Phase A — every logged turn)

| user_id | user_text | assistant_text |
|---------|-----------|----------------|
| 1 | My favorite planet is Mars. | Mars is the fourth planet... |
| 1 | What color is it? | Mars appears red because... |

**`memories`** (Phase B — only high-importance extracted facts)

| user_id | memory_text | importance |
|---------|-------------|------------|
| 1 | Favorite planet is Mars | 8 |

**`summaries`** — empty until Phase C (`MEMORY_SUMMARY_CRON=1`).

---

### What is skipped (no extraction trigger)

Phase B extraction only runs when **all** of the following are true:

1. `MEMORY_EXTRACTION=1`
2. Memory service `ready`
3. A conversational turn was **logged** to `conversations`

These paths **do not** log → **no extraction**:

| `reply_path` | Example | `memory_store` |
|--------------|---------|----------------|
| `volume` | "Turn up the volume" | (no memory fields / early return) |
| `alarm` | "Set alarm for 7 AM" | `skipped` |
| `servo_360` | "Do a 360" | `skipped` |
| `recap` | "What did we just talk about?" | `skipped` (recap queries not logged) |
| `recap_blocked_no_face` | recap with no face | `skipped` |
| `llm` with no face | generic query, face not recognized | `no_viewer` |

STT fragments are also skipped: `memory_store: "skipped_fragment"`.

---

### Validation checklist (Phase B)

1. `GET /api/status` → `memory.extraction_enabled: true`
2. Server log on boot: `Memory layer ready (PostgreSQL)`
3. Voice session with recognized face → `memory_store: "queued"`
4. Server log after turn: `Memory queued conversation log user_id=...`
5. Server log (async): `Extracted N memories for user_id=...`
6. SQL: `SELECT * FROM memories WHERE user_id = 1;`
7. Next voice turn → `memory_facts` > 0 in latency log
8. Ask a cross-session question that only a stored fact can answer

### Why you may see no rows in `memories`

- `MEMORY_EXTRACTION` not set or server not restarted after setting it.
- Turn was not logged (command path, no face, STT fragment, recap question).
- Extractor returned invalid JSON (check server logs for `Memory extraction failed`).
- All proposed items scored below `MEMORY_MIN_IMPORTANCE`.
- Conversation was too trivial ("hi", "thanks") — model assigns low importance.

### Phase B status in this project

**Implemented and wired** — not a future build. The write hook (`after_conversation_logged`), read hook (`_fetch_top_memories` in `load_context`), schema, and env flags all exist. Default deployment has extraction **off** (`MEMORY_EXTRACTION=0`) until you enable it.

---

## Phase C - Daily Summary Memory (Optional, Feature Flag)

### Goal

Create compact day-level memory so NiNO can recall prior sessions naturally.

### What Phase C Does

- Summarizes per-user conversations for prior day(s).
- Stores one summary per user per date.
- Later used as compressed context before responding.

### Table Used

- `summaries` (writes + reads)

### Required Flag

- `MEMORY_SUMMARY_CRON=1`

### Why You May See No Rows

- Flag not enabled.
- No qualifying conversation history for the target day.
- Summary already exists for that user/day (unique constraint).

---

## Phase D - Semantic Memory (Planned)

### Goal

Retrieve relevant memories by meaning, not only exact keyword overlap.

### Planned Behavior

- Add embeddings for memory items.
- Run similarity search for user query embeddings.
- Merge semantic hits with importance-ranked memory.

### Table Impact

- Extends `memories` with embedding/vector data.

### Status

- Planned, not active in the current implementation.

---

## Feature Flags and Environment

Use these environment variables:

- `DATABASE_URL`  
Enables PostgreSQL memory service. Without this, no DB memory writes occur.
- `MEMORY_EXTRACTION` (default `0`)  
Set `1` to enable Phase B writes to `memories`.
- `MEMORY_SUMMARY_CRON` (default `0`)  
Set `1` to enable Phase C writes to `summaries`.
- `MEMORY_MIN_IMPORTANCE` (default `5`)  
  Minimum score accepted when writing long-term memories.
- `MEMORY_TOP_MEMORIES` (default `10`)  
  Max facts loaded from `memories` before each reply (Phase B read path).
- `MEMORY_RECENT_TURNS` (default `10`)  
  Max recent `conversations` turns loaded before each reply (Phase A, also used in recap).

---

## Quick "What Should I Expect?" Matrix

- **Only `DATABASE_URL` enabled** → `users` and `conversations` should populate.
- **`DATABASE_URL` + `MEMORY_EXTRACTION=1`** → `memories` should start populating.
- **`DATABASE_URL` + `MEMORY_SUMMARY_CRON=1`** → `summaries` should start populating.
- **All enabled** → all tables can populate based on usage and data quality.

---

## Validation Checklist

1. Confirm memory service is ready in server startup logs.
2. Verify recognized user flow is happening (face identity not unknown).
3. Confirm conversation paths are being logged (not only command paths).
4. Enable one feature flag at a time (Phase B, then Phase C).
5. Check table row counts after each test session.

---

## Practical Interpretation for Current Behavior

If you currently see data only in `users` and `conversations`, that typically means:

- Phase A is functioning correctly.
- Phase B and Phase C are either disabled or not yet triggered by your recent usage pattern.

This is expected unless those feature flags are explicitly enabled and exercised.