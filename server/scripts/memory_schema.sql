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

CREATE TABLE IF NOT EXISTS summaries (
    id BIGSERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    summary_date DATE NOT NULL,
    summary_text TEXT NOT NULL,
    UNIQUE (user_id, summary_date)
);

CREATE INDEX IF NOT EXISTS idx_summaries_user_date
    ON summaries (user_id, summary_date DESC);
