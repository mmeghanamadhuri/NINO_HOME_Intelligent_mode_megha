-- NiNO memory layer schema (context.md)
-- Apply: psql "$DATABASE_URL" -f server/scripts/memory_schema.sql

CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    face_id VARCHAR(128) UNIQUE NOT NULL,
    name VARCHAR(100) NOT NULL,
    first_seen TIMESTAMP DEFAULT NOW(),
    last_seen TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS conversations (
    id BIGSERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    timestamp TIMESTAMP DEFAULT NOW(),
    user_text TEXT NOT NULL,
    assistant_text TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_conversations_user_ts
    ON conversations (user_id, timestamp DESC);

CREATE TABLE IF NOT EXISTS memories (
    id BIGSERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    memory_text TEXT NOT NULL,
    importance INTEGER DEFAULT 5,
    created_at TIMESTAMP DEFAULT NOW(),
    last_used TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_memories_user_importance
    ON memories (user_id, importance DESC, created_at DESC);

ALTER TABLE memories ADD COLUMN IF NOT EXISTS memory_key VARCHAR(64);
ALTER TABLE memories ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT NOW();

CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_user_key
    ON memories (user_id, memory_key)
    WHERE memory_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS summaries (
    id BIGSERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    summary_date DATE NOT NULL,
    summary_text TEXT NOT NULL,
    UNIQUE (user_id, summary_date)
);

CREATE INDEX IF NOT EXISTS idx_summaries_user_date
    ON summaries (user_id, summary_date DESC);

CREATE TABLE IF NOT EXISTS alarms (
    id VARCHAR(12) PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    fire_at TIMESTAMP NOT NULL,
    label VARCHAR(120) DEFAULT '',
    person_name VARCHAR(64) DEFAULT '',
    created_at TIMESTAMP DEFAULT NOW(),
    fired BOOLEAN DEFAULT FALSE,
    priority INTEGER DEFAULT 1,
    category VARCHAR(32) DEFAULT 'general',
    requires_ack BOOLEAN DEFAULT FALSE,
    ack_state VARCHAR(32) DEFAULT 'none',
    last_fired_at TIMESTAMP,
    next_repeat_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_alarms_user_fire
    ON alarms (user_id, fire_at);

CREATE INDEX IF NOT EXISTS idx_alarms_pending
    ON alarms (fire_at)
    WHERE fired = FALSE OR ack_state NOT IN ('none', '', 'confirmed');
