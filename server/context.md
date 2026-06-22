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

Phase 1

```text
Create PostgreSQL tables
```

Phase 2

```text
Store all conversations
```

Phase 3

```text
Implement memory extraction
```

Phase 4

```text
Implement daily summaries
```

Phase 5

```text
Build context retrieval service
```

Phase 6

```text
Inject memory into Qwen prompts
```

Phase 7

```text
Add pgvector semantic search
```

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
