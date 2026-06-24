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

## Phase A - Conversation Memo

## ry (Active Core)

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

- User preferences
- Repeated interests
- Stable personal details

### What

- Phase B Does

After a conversation is logged, a background extraction step asks the LLM to produce structured memory items and importance scores.

Only high-value entries (based on threshold) are inserted.

### Table Used`memories` (writes + future reads)

### Required Flag

- `MEMORY_EXTRACTION=1`

### Why You May See No Rows

- Flag not enabled.
- Extractor output invalid/empty.
- Importance below threshold (`MEMORY_MIN_IMPORTANCE`).

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

---

## Quick "What Should I Expect?" Matrix

- **Only `DATABASE_URL` enabled** -> `users` and `conversations` should populate.
- `**DATABASE_URL` + `MEMORY_EXTRACTION=1`** -> `memories` should start populating.
- `**DATABASE_URL` + `MEMORY_SUMMARY_CRON=1`** -> `summaries` should start populating.
- **All enabled** -> all tables can populate based on usage and data quality.

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