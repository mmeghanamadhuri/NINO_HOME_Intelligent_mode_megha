# Context & Memory Changes — 25 June 2026

This document summarizes the context, recap, and memory work done in the NiNO voice server on **25 June 2026**. It complements [`context_phase.md`](../context_phase.md) (Phases A–D roadmap) and [`DBP.md`](DBP.md) (database flow).

---

## Goals

1. Stop junk and ephemeral content from polluting PostgreSQL (`conversations`, `memories`).
2. Separate **session recap** (what we talked about) from **personal memory** (birthday, favorites).
3. Replace brittle regex-only memory routing with **LLM classification** (`recall` / `store` / `chat`).
4. Fix recap **hallucinations** when the user asks about a topic that was never in DB.
5. Tighten prompt injection so unrelated stored facts do not leak into replies.

---

## New voice routing order

```
STT
  → volume / echo / garbled rejection
  → alarms / servo
  → conversation recap (before memory LLM)
  → LLM memory turn (recall / store)
  → identity question
  → general LLM chat
```

**Why recap moved before memory LLM:** phrases like *"hope we are talking about microcontroller"* were misclassified as `memory_llm_recall` and answered from personal facts instead of conversation history.

---

## Files changed or added

| File | Role |
|------|------|
| `server/llm_service.py` | Topic recap detection, `recap_not_found` reply, focused recap prompts, `analyze_memory_turn`, store/recall reply helpers |
| `server/voice_service.py` | Routing order, topic-filtered recap context, `recap_not_found` path, STT rejection, volume fix, `load_context(query_text=…)` |
| `server/memory_service.py` | LLM memory turn handler, keyed upsert, query-scoped memory load, preference sync, extraction hooks |
| `server/memory_filters.py` | **New** — deny-list logging, junk filters, recall key aliases, `enrich_llm_memory_text`, query memory filtering |
| `server/scripts/memory_schema.sql` | `memory_key`, `updated_at`, unique index per user+key |
| `server/scripts/cleanup_memories.sql` | **New** — manual SQL to purge polluted rows |
| `server/test_memory_filters.py` | **New/expanded** — recap, topic, filter, enrichment tests |
| `server/test_llm_memory_turn.py` | **New** — LLM memory classification tests |
| `server/test_volume_command.py` | Volume false-positive fix tests |

---

## 1. Conversation recap (session context)

### Expanded recap detection

More patterns in `is_conversation_recap_question()`:

- *"what we are discussing"*, *"please give me the context"*
- *"we are talking about X"*, *"aren't we discussing"*
- *"hope we are talking about …"*

### Topic-focused recap (assumed prior discussion)

New helpers in `llm_service.py`:

| Function | Purpose |
|----------|---------|
| `is_assumed_prior_topic_question()` | User implies a topic was already being discussed |
| `extract_recap_focus_topic()` | Pull topic from e.g. *"we are talking about trigonometry right?"* |
| `recap_turn_matches_topic()` | Filter DB turns — multi-word topics require all significant tokens |
| `recap_topic_not_found_reply()` | Deterministic reply when topic is absent from history |
| `normalize_recap_focus_topic()` | Clean STT noise (`right`, `correct`, punctuation) |

**Patterns now include:** `So, we are talking about…`, `Yesterday/Today we are discussing about…`, `we are talking about…` (comma-tolerant).

### `recap_not_found` path

When the user assumes prior discussion of topic **X** but no matching rows exist in `conversations`:

- **Reply path:** `recap_not_found` (visible in `latency_log.json`)
- **Reply (deterministic, no LLM):**  
  *"I don't have {topic} in our conversation history yet. Shall we discuss it now?"*
- **No educational briefing** invented from general knowledge

### Topic-filtered recap context

`_recap_context_from_recent_turns()` in `voice_service.py`:

- Skips prior recap questions in history
- When `focus_topic` is set, only includes turns where user/assistant text matches the topic
- Returns `None` if no matches → triggers `recap_not_found`

### Focused recap LLM prompts

`answer_conversation_recap()` with `focus_topic`:

- Stricter history rules — only mention the asked topic
- Safety net: if `focus_topic` set but history empty → `recap_topic_not_found_reply()` (no LLM call)

### Bugs fixed (recap)

| Before | After |
|--------|-------|
| *"trigonometry right? brief me"* → LLM invented trigonometry lecture (`reply_path: recap`) | `recap_not_found` — honest "not in history" |
| Recap on microcontroller pulled in biryani/birthday from other turns | Topic filter limits context |
| *"heights and distances"* topic swallowed whole sentence | `extract_recap_focus_topic` fixed for `right? Could you brief…` |
| CEO / microcontroller recap hit `memory_llm_recall` | Recap runs first in routing |

### Verified log example (working)

```json
{
  "heard": "Yesterday we are discussing about trigonometry right, could you please explain about that?",
  "reply_text": "Chakri, I don't have trigonometry in our conversation history yet. Shall we discuss it now?",
  "reply_path": "recap_not_found"
}
```

---

## 2. LLM-driven personal memory

### `analyze_memory_turn()` (`llm_service.py`)

Single Ollama call classifies each turn:

| Action | When |
|--------|------|
| `recall` | Personal facts — birthday, favorites, job, etc. |
| `store` | User explicitly shares durable personal info |
| `chat` | General Q&A, recap, jokes, commands — **not** memory |

Returns JSON: `action`, `recall_keys`, `store[]` with `key`, `memory`, `importance`.

### `handle_llm_memory_turn()` (`memory_service.py`)

- Loads known `memory_key` list for user
- On `recall` → `fetch_memories_for_keys` → `answer_memory_recall_reply`
- On `store` → `store_llm_memory_items` → `answer_memory_store_ack`
- On `chat` → returns `None` (falls through to general LLM)

**Reply paths:** `memory_llm_recall`, `memory_llm_store`

### Schema: keyed memories

```sql
ALTER TABLE memories ADD COLUMN IF NOT EXISTS memory_key VARCHAR(64);
ALTER TABLE memories ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT NOW();
CREATE UNIQUE INDEX idx_memories_user_key ON memories (user_id, memory_key) WHERE memory_key IS NOT NULL;
```

Enables upsert per slot (`favorite_food`, `birthdate`, etc.) instead of duplicate text rows.

### Key aliases (`memory_filters.py`)

- `canonical_preference_key()` — e.g. drink ↔ beverage
- `memory_key_lookup_candidates()` — tries aliases on recall
- Fixes `favorite_drink` vs `favorite_beverage` mismatch

### Store validation

- `enrich_llm_memory_text()` — expands terse LLM output (*"Biryani"* → *"Favorite food is biryani"*)
- `is_valid_llm_memory_item()` — rejects junk, ungrounded, or question-shaped text
- `is_junk_memory_text()` — blocks jokes, assistant boilerplate, fragments

---

## 3. Conversation logging (deny-list)

**Old approach:** allow-list — easy to miss cases, jokes slipped into DB.

**New approach:** `conversation_log_skip_reason()` in `memory_filters.py` — log **all** real chat unless:

| Skip reason | Examples |
|-------------|----------|
| `skipped_recap` | Context/recap questions |
| `skipped_ephemeral` | Jokes, trivia |
| `skipped_recall` | Pattern-based recall (legacy path) |
| `skipped_alarm_*` | Alarms, follow-ups |
| `skipped_tts_echo` | TTS heard back on mic |
| `skipped_fragment` | STT garbage |
| `skipped_{reply_path}` | alarm, volume, recap, memory_llm_recall, etc. |

**Logged paths:** `llm`, `identity_llm`, `memory_llm_store`  
**Not logged:** `recap`, `recap_not_found`, `memory_llm_recall`, alarms, volume, servo

---

## 4. Context injection quality

### Query-scoped memory load

`load_context(display_name, query_text=user_text)`:

- `filter_memories_for_query()` — only inject memories relevant to current question
- `filter_recent_turns_for_prompt()` — drops recap noise and irrelevant turns from prompt block

### Tighter general LLM prompts

`answer_voice_query()` memory rules now say:

- Use **only** facts relevant to the current question
- Do not mention birthdays/hobbies/jokes unless asked
- Answer only what was asked

---

## 5. Input quality & misc fixes

### STT rejection (`voice_service.py`)

Early exit for:

- `is_likely_tts_echo()` — assistant text echoed on mic
- `is_unintelligible_stt()` — garbled transcripts

Reply path: `stt_rejected`

### Volume false positive

*"comes **up**"* no longer triggers volume increase — `parse_volume_command()` requires `"volume"` in utterance and treats bare `"up"` only after volume keywords.

### Birthday / preference patterns

Expanded STT-tolerant patterns in `memory_filters.py` for birthdate and preference updates (partially superseded by LLM memory turn for many cases).

---

## 6. Database cleanup

`server/scripts/cleanup_memories.sql` — manual purge for:

- Short/junk memory rows
- LLM hallucinations in `memories`
- Non-conversation Q&A pollution in `conversations`

Run when needed:

```bash
psql "postgresql://nino:nino@127.0.0.1:5432/nino_memory" -f server/scripts/cleanup_memories.sql
```

---

## 7. Tests

```bash
cd server && python -m unittest test_memory_filters test_llm_memory_turn test_volume_command -q
```

Covers:

- Recap phrase detection
- Topic extraction (trigonometry, sea programming, heights and distances)
- `recap_turn_matches_topic` strict matching
- `recap_topic_not_found_reply` wording
- LLM memory JSON parsing
- Volume command edge cases

---

## 8. Known gaps (not fixed today)

Documented for follow-up — **not implemented** in this session:

| Gap | Symptom |
|-----|---------|
| **No dialogue / open-intent state** | *"Yes"* / *"Please explain about that"* after `recap_not_found` does not continue trigonometry |
| **`recap_not_found` not logged** | Offer to discuss is not in `conversations` for next turn |
| **Vague follow-up → wrong path** | *"explain that"* can hit `memory_llm_recall` (e.g. favorite food) |
| **General recap drift** | Non-topic-focused recap still summarizes full recent history |
| **Phase C / D** | Daily summaries and pgvector semantic memory unchanged |

See prior discussion for a phased design (open intents, follow-up resolver, logging recap offers).

---

## 9. How to verify after deploy

1. **Restart server** after code changes.
2. Ask: *"So, we are talking about trigonometry right? Could you brief me?"*  
   → Expect `reply_path: recap_not_found`, no invented lecture.
3. Ask: *"What is my favorite food?"*  
   → Expect `memory_llm_recall` from `memories` table.
4. Say: *"My favorite food is biryani."*  
   → Expect `memory_llm_store` and row in `memories` with `memory_key = favorite_food`.
5. Check logs:

```bash
psql "postgresql://nino:nino@127.0.0.1:5432/nino_memory" -c \
  "SELECT user_text, assistant_text, timestamp FROM conversations ORDER BY timestamp DESC LIMIT 10;"
```

---

## 10. Related docs

- [`context_phase.md`](../context_phase.md) — Phase A–D memory roadmap
- [`DBP.md`](DBP.md) — PostgreSQL tables and read/write flows
- [`TEST_QUESTIONS.md`](TEST_QUESTIONS.md) — manual voice test phrases

---

*Generated from development session on 25 June 2026.*
