# NiNO Database — Present Working Flow

This document describes **persistent data storage** for the NiNO server: the **PostgreSQL** memory layer, how it is initialized, schema, read/write flows, and what remains in **local JSON/files** instead of the database.

---

## Overview

The server does **not** use an ORM. It talks to PostgreSQL directly via **`psycopg2`** in:

- `server/memory_service.py` — users, conversations, long-term memories, daily summaries
- `server/alarm_service.py` — alarm persistence (when DB is available)

**Activation:** set `DATABASE_URL` in `server/.env` or pass `--database-url` to `app.py`.

```
postgresql://nino:nino@127.0.0.1:5432/nino_memory
```

If `DATABASE_URL` is missing or invalid:

- Memory layer is **disabled** (voice still works, no personalization from DB)
- Alarms fall back to **`server/data/alarms.json`**

---

## Setup and initialization

### Quick setup script

```bash
bash server/scripts/init_memory_db.sh
```

This script:

1. Creates PostgreSQL role `nino` (password `nino`) if missing
2. Creates database `nino_memory` owned by `nino`
3. Applies `server/scripts/memory_schema.sql` via `psql`

Override defaults with env vars: `NINO_DB_NAME`, `NINO_DB_USER`, `NINO_DB_PASS`, `NINO_DB_HOST`, `NINO_DB_PORT`.

### Automatic schema on server start

When `MemoryService.startup()` runs:

1. Validates `DATABASE_URL` (`postgresql://` or `postgres://` prefix required)
2. Connects with `psycopg2`
3. Executes **`memory_schema.sql`** (`CREATE TABLE IF NOT EXISTS …`) — idempotent
4. Sets `_ready = True` and logs Phase B status if extraction is enabled

No separate migration tool; schema changes are applied by editing `memory_schema.sql` and restarting.

---

## Schema

Source: `server/scripts/memory_schema.sql`

```mermaid
erDiagram
    users ||--o{ conversations : has
    users ||--o{ memories : has
    users ||--o{ summaries : has
    users ||--o{ alarms : has

    users {
        serial id PK
        varchar face_id UK
        varchar name
        timestamp first_seen
        timestamp last_seen
    }

    conversations {
        bigserial id PK
        int user_id FK
        timestamp timestamp
        text user_text
        text assistant_text
    }

    memories {
        bigserial id PK
        int user_id FK
        text memory_text
        int importance
        timestamp created_at
        timestamp last_used
    }

    summaries {
        bigserial id PK
        int user_id FK
        date summary_date UK
        text summary_text
    }

    alarms {
        varchar id PK
        int user_id FK
        timestamp fire_at
        varchar label
        varchar person_name
        timestamp created_at
        boolean fired
        int priority
        varchar category
        boolean requires_ack
        varchar ack_state
        timestamp last_fired_at
        timestamp next_repeat_at
    }
```

### Table reference

| Table | Purpose |
|-------|---------|
| **users** | One row per recognized person; keyed by `face_id` slug from display name |
| **conversations** | Turn-by-turn voice chat log (user STT text + assistant reply) |
| **memories** | Long-term facts extracted from conversations (Phase B) |
| **summaries** | Daily rollup text per user (Phase C, optional) |
| **alarms** | Scheduled alarms with medical ack state |

### Indexes

- `conversations (user_id, timestamp DESC)` — recent history lookup
- `memories (user_id, importance DESC, created_at DESC)` — top facts for prompts
- `summaries (user_id, summary_date DESC)` — latest daily summary
- `alarms (user_id, fire_at)` and partial index on pending alarms

### User identity (`face_id`)

`face_id` is derived from the display name:

```python
slug_face_id("Chakri")  # → "chakri"
```

Same slug rules as `FaceService` person folders under `data/faces/`.  
`INSERT … ON CONFLICT (face_id) DO UPDATE` refreshes `name` and `last_seen`.

---

## Memory phases

Configured in `memory_service.py` and `server/.env.example`.

| Phase | Feature | Env flag | Default |
|-------|---------|----------|---------|
| **A** | Recent conversation recall + logging | `DATABASE_URL` | off without URL |
| **B** | LLM extraction of durable facts after each turn | `MEMORY_EXTRACTION` | **on** when DB set |
| **C** | Daily summaries of yesterday's chats | `MEMORY_SUMMARY_CRON` | off |
| **D** | pgvector semantic search | — | not implemented |

### Phase A — Load context before reply

```mermaid
sequenceDiagram
    participant VS as voice_service
    participant MS as memory_service
    participant PG as PostgreSQL
    participant LLM as Ollama

    VS->>MS: load_context(display_name)
    MS->>PG: get_or_create_user(face_id)
    MS->>PG: SELECT recent conversations (LIMIT N)
    MS->>PG: SELECT top memories (importance >= min)
    MS->>PG: SELECT latest summary
    MS-->>VS: LoadedMemoryContext.prompt_block
    VS->>LLM: answer_voice_query(..., memory_context)
```

**Settings:**

- `MEMORY_RECENT_TURNS` (default `10`) — how many past turns to load
- `MEMORY_TOP_MEMORIES` (default `10`) — max facts in prompt
- `MEMORY_MIN_IMPORTANCE` (default `5`) — fact filter threshold

The prompt block tells the LLM to speak in **second person**, lists known facts, recent user lines, and optional daily summary.

**Skipped from logging:**

- STT fragments (incomplete wake/VAD tails)
- Recap/meta questions ("what did we talk about?")

### Phase A — Log conversation after reply

After a successful `llm` or `identity_llm` reply:

1. `log_conversation_for_viewer()` resolves user row
2. Background thread `INSERT INTO conversations`
3. Triggers Phase B hook

```sql
INSERT INTO conversations (user_id, user_text, assistant_text)
VALUES ($1, $2, $3)
```

### Phase B — Long-term memory extraction

Runs in a **background thread** after each logged turn (when `MEMORY_EXTRACTION=1`):

1. Build extraction prompt with user + assistant text
2. Call `ollama_generate()` — expects JSON list of `{ memory, importance }`
3. Insert facts where `importance >= MEMORY_MIN_IMPORTANCE`
4. Skip duplicates (case-insensitive `memory_text` match)

```sql
INSERT INTO memories (user_id, memory_text, importance)
VALUES ($1, $2, $3)
```

### Phase C — Daily summaries

When `MEMORY_SUMMARY_CRON=1`, a daemon thread on startup:

1. Finds users with conversations **yesterday**
2. For each without an existing summary row, calls Ollama to summarize the day's transcript
3. Inserts into `summaries` with `ON CONFLICT DO NOTHING`

```sql
INSERT INTO summaries (user_id, summary_date, summary_text)
VALUES ($1, $2, $3)
ON CONFLICT (user_id, summary_date) DO NOTHING
```

Summaries are loaded into the prompt block as "Earlier session summary".

---

## Alarm storage in PostgreSQL

`alarm_service.py` uses the same `DATABASE_URL` (via `memory_service.SETTINGS`).

### Storage mode selection

| Condition | Storage |
|-----------|---------|
| `DATABASE_URL` set + `psycopg2` + memory service `ready` | **PostgreSQL** `alarms` table |
| Otherwise | **`server/data/alarms.json`** |

Status field: `GET /api/alarms` → `"storage": "postgresql"` or `"json"`.

### Read path (startup)

1. `SELECT` all non-expired alarms from `alarms`
2. If DB is empty but `alarms.json` has data → **one-time migration** into PostgreSQL

### Write path

Every add/update/delete:

```sql
INSERT INTO alarms (...) VALUES (...)
ON CONFLICT (id) DO UPDATE SET ...
```

or `DELETE FROM alarms WHERE id = $1`

### Alarm ↔ user link

When a face is recognized at alarm creation time:

- `resolve_alarm_user()` → `ensure_user()` → `user_id` stored on alarm row
- `person_name` stored for spoken messages
- Voice list/cancel can scope to `user_id`

---

## API for database inspection

| Endpoint | Returns |
|----------|---------|
| `GET /api/status` | `memory.enabled`, `memory.ready`, `memory.table_counts`, `alarms.storage` |
| `GET /api/memory/stats` | Row counts: `users`, `conversations`, `memories`, `summaries`, `alarms` |

Example `table_counts`:

```json
{
  "users": 3,
  "conversations": 142,
  "memories": 28,
  "summaries": 2,
  "alarms": 5
}
```

---

## End-to-end data flow (voice + memory)

```mermaid
flowchart TD
    A[ESP sends WAV] --> B[STT transcript]
    B --> C{Recognized face?}
    C -->|yes| D[load_context → users + conversations + memories + summaries]
    C -->|no| E[Generic LLM reply]
    D --> F[Ollama with memory prompt block]
    E --> F
    F --> G[TTS reply WAV]
    G --> H{Loggable turn?}
    H -->|yes| I[INSERT conversations]
    I --> J{MEMORY_EXTRACTION?}
    J -->|yes| K[Ollama extract facts → INSERT memories]
    H -->|no| L[Skip log]
```

---

## Environment variables (database)

| Variable | Purpose | Default |
|----------|---------|---------|
| `DATABASE_URL` | PostgreSQL connection string | unset = disabled |
| `MEMORY_EXTRACTION` | Phase B on/off | `1` when URL set |
| `MEMORY_MIN_IMPORTANCE` | Min score to store a fact | `5` |
| `MEMORY_TOP_MEMORIES` | Facts loaded per prompt | `10` |
| `MEMORY_RECENT_TURNS` | Conversation turns loaded | `10` |
| `MEMORY_SUMMARY_CRON` | Phase C daily summaries | `0` (off) |

CLI equivalents in `app.py`:

- `--database-url`
- `--memory-extraction` / `--no-memory-extraction`
- `--memory-min-importance`
- `--memory-top-memories`

---

## What is NOT in PostgreSQL

These remain **local filesystem** data managed outside the DB layer:

| Location | Content | Managed by |
|----------|---------|------------|
| `server/data/faces/` | Face sample JPEGs | `face_service.py` |
| `server/data/face_embeddings.json` | SFace 128-D vectors | `face_service.py` |
| `server/data/models/` | YuNet / SFace ONNX files | `face_service.py` |
| `server/data/latency_log.json` | Voice timing audit log | `app.py` |
| `server/data/alarms.json` | Alarm fallback + migration source | `alarm_service.py` |
| `server/data/labels.json` | Legacy labels | face pipeline |
| `server/data/person_thresholds.json` | Legacy per-person thresholds | face pipeline |

Face recognition identity is **file-based**; PostgreSQL `users.face_id` is a **slug mirror** of the registered name, not a foreign key to image files.

---

## Dependencies

```
psycopg2-binary>=2.9.9   # in server/requirements.txt
```

PostgreSQL server must be running and reachable at the host/port in `DATABASE_URL`.

---

## Failure behavior

| Failure | Effect |
|---------|--------|
| Invalid `DATABASE_URL` | Memory disabled; warning logged; alarms use JSON |
| `psycopg2` not installed | Memory disabled; install hint logged |
| DB connection error on startup | `_ready = False`; voice works without memory |
| Log/extract thread error | Warning logged; reply still delivered to user |
| DB down mid-session | `load_context` returns `None`; logging skipped for that turn |

The server is designed to **degrade gracefully**: voice, vision, and alarms (via JSON) continue without PostgreSQL.

---

## Manual operations

```bash
# Apply schema manually
psql "$DATABASE_URL" -f server/scripts/memory_schema.sql

# Inspect tables
psql "$DATABASE_URL" -c "SELECT COUNT(*) FROM users;"
psql "$DATABASE_URL" -c "SELECT * FROM conversations ORDER BY timestamp DESC LIMIT 5;"

# Clear a user's history (example)
psql "$DATABASE_URL" -c "DELETE FROM conversations WHERE user_id = 1;"
```

`ON DELETE CASCADE` on foreign keys means deleting a `users` row removes their conversations, memories, summaries, and alarms.
