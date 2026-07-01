# Phase B Detailed Guide (Long-Term Memory Extraction)

This document explains Phase B in detail: what it does, when it runs, how it stores data, and what outcomes to expect in real usage.

---

## 1) What Phase B Is

Phase B is the **long-term memory extraction layer** for NiNO.

After a normal conversation turn is saved (Phase A), Phase B tries to extract durable facts from that turn and store them in the `memories` table.

In short:

- **Phase A** = short-term continuity from recent turns
- **Phase B** = long-term user facts for personalization over time

---

## 2) Main Goal

Remember high-value user information that should survive across sessions, such as:

- preferences ("I prefer tea over coffee")
- stable routines ("I study at night")
- long-term interests ("I am preparing for robotics interviews")
- relevant constraints ("I am allergic to peanuts")

This helps NiNO give more context-aware replies in later interactions.

---

## 3) Prerequisites (When Phase B Can Run)

Phase B runs only if all conditions below are true:

1. Memory service is enabled (`DATABASE_URL` is valid).
2. PostgreSQL memory service started successfully.
3. A conversation turn was logged (Phase A path).
4. `MEMORY_EXTRACTION=1` is enabled.

If any of these are missing, `memories` will not be populated.

---

## 4) End-to-End Execution Flow

### Step A - Conversation turn is saved first

The system logs a pair:

- user speech text (`user_text`)
- assistant reply text (`assistant_text`)

This insert goes to `conversations`.

### Step B - Post-log hook triggers extraction

After a successful insert, a follow-up hook checks whether Phase B is enabled.

If enabled, it starts an asynchronous worker.

### Step C - Extraction prompt is built

The worker prepares an LLM prompt asking for structured JSON:

- `memory` (string)
- `importance` (integer 0-10)

### Step D - LLM generates candidate memories

The extractor model (via Ollama) returns candidate entries.

### Step E - Parser and filter stage

Returned items are parsed and validated:

- invalid JSON is rejected
- empty memory text is rejected
- low-importance entries are rejected

Default threshold:

- keep only `importance >= MEMORY_MIN_IMPORTANCE` (default `5`)

### Step F - Valid items are inserted

Each accepted item is inserted into:

- `memories(user_id, memory_text, importance, created_at)`

This completes Phase B for that turn.

---

## 5) Why Phase B Is Asynchronous

Phase B runs in a background thread to avoid slowing down speech response.

Design intent:

- user hears assistant reply without waiting for extraction
- extraction happens after response pipeline continues

So Phase B should have minimal user-visible latency impact.

---

## 6) Data Model Used by Phase B

Phase B primarily writes to:

- `memories`

Key columns:

- `user_id` - links memory to person in `users`
- `memory_text` - extracted fact
- `importance` - relevance score (0-10)
- `created_at` - insertion timestamp
- `last_used` - available for future ranking/usage updates

Ordering for retrieval usually favors:

1. higher `importance`
2. newer `created_at`

---

## 7) Good vs Bad Memory Candidates

### Good (likely to be stored)

- "My favorite planet is Mars."
- "I work night shifts."
- "I am learning embedded C for interviews."
- "I prefer concise explanations."

### Bad (likely filtered out or low importance)

- "What time is it?"
- "Increase volume."
- "Rotate servo."
- one-off weather questions

Reason: these are commands or transient requests, not durable user facts.

---

## 8) Concrete Example 1 (Single Turn)

### Conversation

- User: "I am preparing for a robotics interview and I prefer practical coding examples."
- Assistant: "Great, I can focus on robotics topics and practical coding-style answers."

### Possible extractor output

```json
[
  {"memory": "User is preparing for a robotics interview.", "importance": 8},
  {"memory": "User prefers practical coding examples.", "importance": 7},
  {"memory": "Conversation happened today.", "importance": 2}
]
```

### What gets inserted

- First memory inserted (importance 8)
- Second memory inserted (importance 7)
- Third memory rejected (importance 2 < threshold 5)

---

## 9) Concrete Example 2 (No Insert Case)

### Conversation

- User: "Set volume to 60."
- Assistant: "Done, volume set to 60."

### Possible extractor output

```json
[
  {"memory": "User asked to set volume to 60.", "importance": 2}
]
```

### Result

No row inserted to `memories` (below threshold).

This is expected behavior.

---

## 10) Common Reasons `memories` Stays Empty

1. `MEMORY_EXTRACTION` not enabled (`0` by default).
2. LLM extraction output not parseable JSON.
3. Extracted items all below importance threshold.
4. Interactions are mostly commands, not profile facts.
5. Background extraction errors (model, timeout, API URL issues).

---

## 11) How to Enable and Verify Phase B

Set environment variables before starting server:

```bash
export DATABASE_URL=postgresql://user:pass@host:5432/dbname
export MEMORY_EXTRACTION=1
export MEMORY_MIN_IMPORTANCE=5
```

Then run a few fact-rich conversations and check:

```sql
SELECT id, user_id, memory_text, importance, created_at
FROM memories
ORDER BY created_at DESC
LIMIT 20;
```

If rows appear, Phase B is active and writing correctly.

---

## 12) Relationship with Other Phases

- **Depends on Phase A** (needs logged conversation turns).
- **Feeds later prompt context** (long-term personalization).
- **Complements Phase C** (daily summaries).
- **Can later combine with Phase D** semantic retrieval.

So Phase B is the bridge between "recent context" and "persistent user profile memory."

---

## 13) Practical Testing Script (Recommended)

Use these 5 prompts in one session:

1. "I am preparing for a robotics job interview."
2. "I prefer short answers with practical examples."
3. "My favorite learning time is late night."
4. "I am focusing on embedded C and RTOS topics."
5. "Please remember I like step-by-step debugging."

Expected outcome:

- `conversations` grows for each turn
- `memories` receives multiple high-importance facts

If `conversations` grows but `memories` does not, Phase B configuration or extraction output quality is the issue.

---

## 14) Summary

Phase B is a background LLM extraction pipeline that converts conversation turns into durable, scored user memories.

It is intentionally optional and controlled by feature flags so teams can enable it safely after validating Phase A reliability.
