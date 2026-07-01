# Vision + Voice Assistant Memory Workflow

## Current System

### Components Already Available

```text
Face Recognition : SFace
Speech-To-Text   : ElevenLabs STT
LLM              : Qwen
Text-To-Speech   : ElevenLabs TTS
Database         : PostgreSQL
Device           : ESP32-P4
```

---

# System Goal

Create a personalized assistant that:

* Recognizes users by face
* Remembers previous conversations
* Stores important information about users
* Retrieves memories automatically
* Continues conversations across multiple days
* Builds long-term relationships with users

Example:

Day 1:

User:

> Tell me about Mars.

Assistant:

> Mars is the fourth planet from the Sun.

Day 2:

Assistant:

> Welcome back. Yesterday we discussed Mars. Would you like to continue our discussion?

---

# Complete System Architecture

```text
Camera
   |
   v
SFace Recognition
   |
   v
User Identification
   |
   v
PostgreSQL Memory Service
   |
   +------------------+
   |                  |
   | Retrieve Memory  |
   | Retrieve History |
   | Retrieve Summary |
   |                  |
   +------------------+
           |
           v
         Qwen
           |
           v
    ElevenLabs TTS
           |
           v
        ESP32-P4
```

---

# Database Design

## Users Table

Stores recognized users.

```sql
CREATE TABLE users (
    id SERIAL PRIMARY KEY,
    face_id VARCHAR(128) UNIQUE,
    name VARCHAR(100),
    first_seen TIMESTAMP DEFAULT NOW(),
    last_seen TIMESTAMP DEFAULT NOW()
);
```

Example:

```text
face_id = user_001
name = Karthik
```

---

## Conversations Table

Stores every conversation exchange.

```sql
CREATE TABLE conversations (
    id BIGSERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id),
    timestamp TIMESTAMP DEFAULT NOW(),
    user_text TEXT,
    assistant_text TEXT
);
```

Example:

```text
User:
Tell me about Mars

Assistant:
Mars is the fourth planet from the Sun.
```

---

## Memories Table

Stores long-term user information.

```sql
CREATE TABLE memories (
    id BIGSERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id),
    memory_text TEXT,
    importance INTEGER DEFAULT 5,
    created_at TIMESTAMP DEFAULT NOW(),
    last_used TIMESTAMP
);
```

Examples:

```text
Likes astronomy
Favorite planet is Mars
Interested in robotics
Works on ESP32-P4
```

---

## Daily Summaries Table

Stores summarized conversation history.

```sql
CREATE TABLE summaries (
    id BIGSERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id),
    summary_date DATE,
    summary_text TEXT
);
```

Example:

```text
2026-06-20

Discussed:
- Mars
- SpaceX
- Astronomy
```

---

# Runtime Workflow

## Step 1: User Appears

Camera captures image.

SFace performs recognition.

Result:

```text
face_id = user_001
```

---

## Step 2: Find User

Query PostgreSQL.

```sql
SELECT *
FROM users
WHERE face_id='user_001';
```

Result:

```text
id = 1
name = Karthik
```

---

## Step 3: Speech Input

User speaks.

Example:

```text
Hello
```

ElevenLabs STT converts speech into text.

---

## Step 4: Load User Context

Before calling Qwen, retrieve relevant information.

### Load Recent Conversations

```sql
SELECT *
FROM conversations
WHERE user_id=1
ORDER BY timestamp DESC
LIMIT 10;
```

---

### Load Important Memories

```sql
SELECT *
FROM memories
WHERE user_id=1
ORDER BY importance DESC
LIMIT 20;
```

---

### Load Latest Summary

```sql
SELECT *
FROM summaries
WHERE user_id=1
ORDER BY summary_date DESC
LIMIT 1;
```

---

# Context Builder

Build a structured prompt.

Example:

```text
Current User:
Karthik

Known Memories:
- Likes astronomy
- Favorite planet is Mars
- Interested in robotics

Yesterday Summary:
Discussed Mars and SpaceX.

Recent Conversations:
User: Tell me about Mars.
Assistant: Mars is the fourth planet...

Current Input:
Hello
```

---

# LLM Processing

Send the constructed context to Qwen.

Qwen now understands:

* Who the user is
* What they like
* What they discussed recently
* What they discussed yesterday

---

# Example Response

Qwen generates:

```text
Welcome back Karthik.

Yesterday we discussed Mars and SpaceX.

Would you like to continue our conversation about Mars?
```

---

# Speech Output

Qwen response is sent to:

```text
ElevenLabs TTS
```

Audio is generated and streamed to:

```text
ESP32-P4
```

---

# Conversation Logging

After every interaction:

```sql
INSERT INTO conversations
(
    user_id,
    user_text,
    assistant_text
)
VALUES
(
    1,
    'Tell me about Mars',
    'Mars is the fourth planet from the Sun'
);
```

---

# Memory Extraction

After the conversation is complete, perform a second Qwen call.

Purpose:

Extract useful long-term memories.

Prompt:

```text
Extract useful long-term memories.

Conversation:

User:
My favorite planet is Mars.

Assistant:
Interesting.

Return JSON only.
```

---

# Memory Extraction Result

```json
[
  {
    "memory":"Favorite planet is Mars",
    "importance":9
  }
]
```

---

# Store Memory

```sql
INSERT INTO memories
(
    user_id,
    memory_text,
    importance
)
VALUES
(
    1,
    'Favorite planet is Mars',
    9
);
```

---

# Importance Scoring

Not everything should be remembered.

Examples:

```text
Favorite planet is Mars          -> 9
Likes astronomy                  -> 8
Works on ESP32-P4                -> 8
Asked weather today              -> 1
Battery is at 50 percent         -> 0
```

Only high-value memories should be retrieved regularly.

---

# Daily Summary Generation

Run once per day.

Collect all conversations for a user.

Prompt:

```text
Summarize today's conversations.
```

Example output:

```text
Discussed:
- Mars
- Astronomy
- SpaceX
```

Store result:

```sql
INSERT INTO summaries
(
    user_id,
    summary_date,
    summary_text
)
VALUES
(
    1,
    CURRENT_DATE,
    'Discussed Mars, Astronomy and SpaceX'
);
```

---

# Future Enhancement: Semantic Memory Search

After the system is stable:

Install:

```text
pgvector
```

Add vector column:

```sql
ALTER TABLE memories
ADD COLUMN embedding VECTOR(768);
```

Benefits:

* Retrieve related memories
* Understand semantic similarity
* Better contextual recall

Example:

User says:

```text
Tell me again about planets
```

System can retrieve:

```text
Favorite planet is Mars
Discussed Mars yesterday
Interested in astronomy
```

even when the word "Mars" is not spoken.

---

# Development Order

Implementation is split into **seven technical phases** (database design above) rolled out as **four delivery phases (A–D)** on the NiNO Python server. See also [`context_main.md`](context_main.md) for what is already running today.

---

## Delivery phases (NiNO server)

### Phase A — Conversation memory (in progress)

**Goal:** Same-day continuity and recent-thread recall.

```text
PostgreSQL users + conversations tables
  → resolve user from SFace display name (face_id slug)
  → load last 5 conversation turns before LLM
  → inject into Qwen/Ollama prompt
  → log exchange after reply (non-blocking)
```

**Server modules:** `memory_service.py`, `llm_service.py`, `voice_service.py`, `app.py`

**Latency budget:** +10–40 ms DB read, +200–500 ms LLM prefill → **~+0.3–0.5 s** total on GPU

**Skip logging for:** volume commands, servo 360, alarm regex paths (not conversational)

---

### Phase B — Long-term memory extraction

**Goal:** Remember durable facts across sessions (“favorite planet is Mars”).

```text
After each logged conversation (background thread, does NOT block TTS):
  → second Qwen call with extraction prompt
  → parse JSON [{ "memory", "importance" }]
  → INSERT INTO memories (importance ≥ 5 only)
  → load top 10 memories by importance before LLM
```

**Latency budget:** **0 ms user-visible** (async post-reply)

**Importance filter:** store 5–10; ignore 0–4 (weather, battery, etc.)

---

### Phase C — Daily summaries & cross-day greetings

**Goal:** “Welcome back — yesterday we discussed Mars and SpaceX.”

```text
Nightly job (or startup catch-up):
  → summarize today's conversations per user via Qwen
  → INSERT INTO summaries

Before LLM + vision greeting_for_face():
  → load latest summary
  → inject into prompt
```

**Latency budget:** +50–100 ms DB read; summary text adds ~+100–300 ms LLM prefill

---

### Phase D — Semantic memory (pgvector)

**Goal:** Recall related facts when the user does not repeat keywords.

```text
ALTER TABLE memories ADD COLUMN embedding VECTOR(768);
  → embed on memory INSERT (background)
  → at query time: embed user_text → nearest-neighbor retrieval
  → merge with importance-ranked memories
```

**Latency budget:** +20–80 ms vector search (small corpus); embedding on write only

---

## Technical phases (build order)

| # | Task | Delivery phase | Status |
|---|------|----------------|--------|
| 1 | Create PostgreSQL tables (`users`, `conversations`, `memories`, `summaries`) | A (all tables); B/C use later columns | A: schema |
| 2 | Store all conversations | A | A: wire |
| 3 | Implement memory extraction | B | Planned |
| 4 | Implement daily summaries | C | Planned |
| 5 | Build context retrieval service | A–C | A: `memory_service.load_context()` |
| 6 | Inject memory into Qwen prompts | A–C | A: recent turns; B: +memories; C: +summary |
| 7 | Add pgvector semantic search | D | Planned |

---

## Assumed latency (full stack)

Baseline today (GPU Ollama + ElevenLabs STT, from `server/data/latency_log.json`):

| Stage | Typical |
|-------|---------|
| STT | ~0.9 s |
| LLM (short prompt) | ~0.2–0.4 s |
| TTS | ~1.2 s |
| **Server total** | **~3 s** |

After all memory phases on GPU:

| Stage | Typical |
|-------|---------|
| STT | ~0.9 s (unchanged) |
| DB context load | ~10–40 ms |
| LLM (with memory prompt) | ~0.5–1.2 s |
| TTS | ~1.2 s (unchanged) |
| Memory extraction | 0 ms visible (background) |
| **Server total target** | **≤ 4.5 s** |

**Rules to protect latency:**

* Cap context: 5 recent turns, 10 memories, 1 summary
* Never block reply on extraction, INSERT, or embedding
* PostgreSQL on same host as FastAPI (not remote)
* Keep GPU Ollama warm (`OLLAMA_KEEP_ALIVE`)

---

## Environment (memory layer)

| Variable | Default | Purpose |
|----------|---------|---------|
| `DATABASE_URL` | — | PostgreSQL connection string; memory disabled if unset |
| `MEMORY_RECENT_TURNS` | `5` | Max conversation pairs loaded into prompt |
| `MEMORY_TOP_MEMORIES` | `10` | Max memories by importance (Phase B) |
| `MEMORY_MIN_IMPORTANCE` | `5` | Store/retrieve threshold (Phase B) |
| `MEMORY_EXTRACTION` | `0` | `1` enables Phase B background extraction |
| `MEMORY_SUMMARY_CRON` | `0` | `1` enables Phase C daily summary on startup |

Init script: `server/scripts/init_memory_db.sh` + `server/scripts/memory_schema.sql`

---

# Final Workflow

```text
Camera
   ↓
SFace Recognition
   ↓
Identify User
   ↓
Load Memories
   ↓
Load Recent Conversations
   ↓
Load Daily Summary
   ↓
Build Context
   ↓
Qwen
   ↓
Generate Response
   ↓
ElevenLabs TTS
   ↓
ESP32-P4 Playback
   ↓
Store Conversation
   ↓
Extract New Memories
   ↓
Update PostgreSQL
   ↓
Generate Daily Summary
```

---

# Expected End Result

The assistant behaves as a persistent companion.

Example:

```text
Welcome back Karthik.

Yesterday we discussed Mars and SpaceX.

You mentioned that Mars is your favorite planet.

Would you like to continue where we left off?
```

The response is generated automatically through:

* Face Recognition
* Conversation History
* Long-Term Memory
* Daily Summaries
* Context Injection into Qwen
* PostgreSQL Memory Retrieval
