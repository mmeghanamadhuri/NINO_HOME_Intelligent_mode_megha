"""PostgreSQL memory layer — users, conversations, context for LLM prompts.

Phase A: recent conversation recall + logging
Phase B: long-term memory extraction (optional, MEMORY_EXTRACTION=1)
Phase C: daily summaries (optional, MEMORY_SUMMARY_CRON=1)
Phase D: pgvector semantic search (future)
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

logger = logging.getLogger(__name__)

try:
    import psycopg2
    import psycopg2.extras
except ImportError:  # pragma: no cover
    psycopg2 = None  # type: ignore[assignment]


def slug_face_id(display_name: str) -> str:
    """Same slug rules as FaceService._person_id."""
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "_", display_name.strip().lower())
    return cleaned.strip("_")


def normalize_database_url(raw: str) -> str:
    """Return a valid PostgreSQL URL or empty string."""
    url = raw.strip().strip('"').strip("'")
    if not url:
        return ""
    if url.startswith("://"):
        return ""
    if not (url.startswith("postgresql://") or url.startswith("postgres://")):
        return ""
    return url


def resolve_memory_display_name(
    viewer_name: str | None,
    *,
    camera_identity_name: str | None = None,
    camera_identity_state: str = "no_face",
) -> str | None:
    """Pick the display name used for PostgreSQL user + conversation logging."""
    for candidate in (viewer_name, camera_identity_name):
        if not candidate:
            continue
        cleaned = str(candidate).strip()
        if cleaned and cleaned.lower() not in {"unknown", "face"}:
            return cleaned
    if camera_identity_state == "recognized" and camera_identity_name:
        cleaned = str(camera_identity_name).strip()
        if cleaned:
            return cleaned
    return None


_STT_FRAGMENT_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"^(?:so,?\s*)?(?:here\s+)?we just (?:discussed|talked)(?:\s+about)?[\s-]*$",
        r"^that we just talked about\.?$",
        r"^(?:what )?we (?:just )?(?:talked|discussed) about[\s-]*$",
        r"^(?:about|and|so|here)[\s-]*$",
        r"^tell me about[\s-]*$",
    )
)


def is_stt_fragment(user_text: str) -> bool:
    """Drop incomplete wake/VAD speech-to-text tails from memory."""
    text = user_text.strip()
    if not text:
        return True
    if text.endswith("-") or text.endswith(" about") or text.endswith(" about-"):
        return True
    if len(text) < 6 and "?" not in text:
        return True
    return any(p.search(text) for p in _STT_FRAGMENT_PATTERNS)


def is_loggable_user_text(user_text: str) -> bool:
    return not is_stt_fragment(user_text)


def filter_recent_turns(
    recent: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Remove STT fragments from session history shown to the LLM."""
    cleaned: list[tuple[str, str]] = []
    for user_text, assistant_text in recent:
        if is_stt_fragment(user_text):
            continue
        cleaned.append((user_text.strip(), assistant_text.strip()))
    return cleaned


def truncate_context_text(text: str, limit: int = 160) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."


@dataclass
class LoadedMemoryContext:
    user_id: int
    face_id: str
    name: str
    prompt_block: str
    recent_turns: int = 0
    memory_count: int = 0
    has_summary: bool = False
    load_seconds: float = 0.0
    recent_history: list[tuple[str, str]] = field(default_factory=list)


_RECAP_TOPIC_PREFIX = re.compile(
    r"^(?:(?:please|can you|could you|would you|kindly)\s+)?"
    r"(?:(?:tell me|explain|describe|say|give me)\s+)?"
    r"(?:(?:about|regarding)\s+)?",
    re.IGNORECASE,
)


def question_to_recap_topic(user_text: str) -> str:
    text = user_text.strip().rstrip("?.! ")
    text = _RECAP_TOPIC_PREFIX.sub("", text).strip()
    if not text:
        text = user_text.strip()
    return truncate_context_text(text, 80)


def build_spoken_recap(
    recent: list[tuple[str, str]],
    *,
    max_topics: int = 3,
) -> str | None:
    """Build a short second-person recap from stored turns (no LLM)."""
    from llm_service import is_conversation_recap_question

    topics: list[str] = []
    seen: set[str] = set()
    for user_text, _assistant_text in recent:
        cleaned = user_text.strip()
        if not cleaned or is_stt_fragment(cleaned):
            continue
        if is_conversation_recap_question(cleaned):
            continue
        topic = question_to_recap_topic(cleaned)
        key = topic.lower()
        if not key or key in seen:
            continue
        seen.add(key)
        topics.append(topic)

    if not topics:
        return None

    topics = topics[-max_topics:]
    if len(topics) == 1:
        return f"You asked about {topics[0]}."
    if len(topics) == 2:
        return f"You asked about {topics[0]} and {topics[1]}."
    body = ", ".join(topics[:-1]) + f", and {topics[-1]}"
    return f"You asked about {body}."


@dataclass
class MemorySettings:
    database_url: str = ""
    recent_turns: int = 5
    top_memories: int = 10
    min_importance: int = 5
    extraction_enabled: bool = False
    summary_cron_enabled: bool = False


SETTINGS = MemorySettings()
_lock = threading.Lock()
_service: MemoryService | None = None


def configure_from_environ() -> None:
    SETTINGS.database_url = normalize_database_url(os.environ.get("DATABASE_URL", ""))
    SETTINGS.recent_turns = max(1, int(os.environ.get("MEMORY_RECENT_TURNS", "5")))
    SETTINGS.top_memories = max(1, int(os.environ.get("MEMORY_TOP_MEMORIES", "10")))
    SETTINGS.min_importance = int(os.environ.get("MEMORY_MIN_IMPORTANCE", "5"))
    SETTINGS.extraction_enabled = os.environ.get("MEMORY_EXTRACTION", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    SETTINGS.summary_cron_enabled = os.environ.get("MEMORY_SUMMARY_CRON", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


class MemoryService:
    def __init__(self) -> None:
        self._ready = False
        self._last_error = ""
        self._schema_checked = False

    @property
    def enabled(self) -> bool:
        return bool(SETTINGS.database_url and psycopg2 is not None)

    @property
    def ready(self) -> bool:
        return self._ready

    def startup(self) -> None:
        configure_from_environ()
        if not SETTINGS.database_url:
            if os.environ.get("DATABASE_URL", "").strip():
                self._last_error = (
                    "DATABASE_URL is set but invalid — use "
                    "postgresql://user:pass@host:5432/dbname"
                )
                logger.warning("Memory layer disabled: %s", self._last_error)
            else:
                logger.info("Memory layer disabled (DATABASE_URL not set)")
            return
        if psycopg2 is None:
            logger.warning(
                "Memory layer disabled — install psycopg2-binary: pip install psycopg2-binary"
            )
            return
        try:
            self._ensure_schema()
            self._ready = True
            logger.info("Memory layer ready (PostgreSQL)")
            if SETTINGS.summary_cron_enabled:
                threading.Thread(
                    target=self._run_summary_catchup_safe,
                    daemon=True,
                    name="memory-summary-catchup",
                ).start()
        except Exception as exc:
            self._ready = False
            self._last_error = str(exc)
            logger.warning("Memory layer unavailable: %s", exc)

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "ready": self._ready,
            "database_url_set": bool(SETTINGS.database_url),
            "recent_turns": SETTINGS.recent_turns,
            "top_memories": SETTINGS.top_memories,
            "extraction_enabled": SETTINGS.extraction_enabled,
            "summary_cron_enabled": SETTINGS.summary_cron_enabled,
            "last_error": self._last_error,
        }

    def load_context(self, display_name: str | None) -> LoadedMemoryContext | None:
        if not self._ready or not display_name:
            return None
        face_id = slug_face_id(display_name)
        if not face_id:
            return None
        t0 = time.perf_counter()
        try:
            with self._connect() as conn:
                user_id = self._get_or_create_user(conn, face_id, display_name.strip())
                recent = self._fetch_recent_conversations(conn, user_id, SETTINGS.recent_turns)
                memories = self._fetch_top_memories(
                    conn, user_id, SETTINGS.top_memories, SETTINGS.min_importance
                )
                summary = self._fetch_latest_summary(conn, user_id)
            recent = filter_recent_turns(recent)
            block = self._format_prompt_block(
                name=display_name.strip(),
                recent=recent,
                memories=memories,
                summary=summary,
            )
            return LoadedMemoryContext(
                user_id=user_id,
                face_id=face_id,
                name=display_name.strip(),
                prompt_block=block,
                recent_turns=len(recent),
                memory_count=len(memories),
                has_summary=bool(summary),
                load_seconds=round(time.perf_counter() - t0, 4),
                recent_history=list(recent),
            )
        except Exception as exc:
            logger.warning("Memory context load failed: %s", exc)
            self._last_error = str(exc)
            return None

    def ensure_user(self, display_name: str) -> LoadedMemoryContext | None:
        """Create/lookup user row only — used when logging if load_context was skipped."""
        if not self._ready:
            return None
        face_id = slug_face_id(display_name)
        if not face_id:
            return None
        try:
            with self._connect() as conn:
                user_id = self._get_or_create_user(conn, face_id, display_name.strip())
            return LoadedMemoryContext(
                user_id=user_id,
                face_id=face_id,
                name=display_name.strip(),
                prompt_block="",
            )
        except Exception as exc:
            logger.warning("ensure_user failed: %s", exc)
            self._last_error = str(exc)
            return None

    def log_conversation_background(
        self,
        user_id: int,
        user_text: str,
        assistant_text: str,
    ) -> None:
        if not self._ready:
            return
        threading.Thread(
            target=self._log_conversation_safe,
            args=(user_id, user_text, assistant_text),
            daemon=True,
            name="memory-log-conversation",
        ).start()
        logger.info(
            "Memory queued conversation log user_id=%s heard=%s",
            user_id,
            user_text[:80],
        )

    def log_conversation_for_viewer(
        self,
        display_name: str | None,
        user_text: str,
        assistant_text: str,
        *,
        existing: LoadedMemoryContext | None = None,
    ) -> str:
        """Log exchange; returns skip reason or 'queued'."""
        if not display_name:
            return "no_viewer"
        if not self._ready:
            return "db_not_ready"
        if not is_loggable_user_text(user_text):
            logger.info("Skipping STT fragment from memory log: %s", user_text[:80])
            return "skipped_fragment"
        ctx = existing or self.ensure_user(display_name)
        if not ctx:
            return "user_resolve_failed"
        self.log_conversation_background(ctx.user_id, user_text, assistant_text)
        return "queued"

    def after_conversation_logged(
        self,
        user_id: int,
        user_text: str,
        assistant_text: str,
    ) -> None:
        """Phase B hook — run after conversation INSERT."""
        if not self._ready or not SETTINGS.extraction_enabled:
            return
        threading.Thread(
            target=self._extract_memories_safe,
            args=(user_id, user_text, assistant_text),
            daemon=True,
            name="memory-extract",
        ).start()

    # ------------------------------------------------------------------ internals

    def _connect(self):
        assert psycopg2 is not None
        return psycopg2.connect(SETTINGS.database_url)

    def _ensure_schema(self) -> None:
        if self._schema_checked:
            return
        schema_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "scripts",
            "memory_schema.sql",
        )
        with self._connect() as conn:
            with conn.cursor() as cur:
                if os.path.isfile(schema_path):
                    cur.execute(open(schema_path, encoding="utf-8").read())
                else:
                    raise FileNotFoundError(f"Schema not found: {schema_path}")
            conn.commit()
        self._schema_checked = True

    def _get_or_create_user(self, conn, face_id: str, name: str) -> int:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (face_id, name, first_seen, last_seen)
                VALUES (%s, %s, NOW(), NOW())
                ON CONFLICT (face_id) DO UPDATE
                    SET name = EXCLUDED.name,
                        last_seen = NOW()
                RETURNING id
                """,
                (face_id, name),
            )
            row = cur.fetchone()
            conn.commit()
            return int(row[0])

    def _fetch_recent_conversations(
        self, conn, user_id: int, limit: int
    ) -> list[tuple[str, str]]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT user_text, assistant_text
                FROM conversations
                WHERE user_id = %s
                ORDER BY timestamp DESC
                LIMIT %s
                """,
                (user_id, limit),
            )
            rows = cur.fetchall()
        return [(str(u), str(a)) for u, a in reversed(rows)]

    def _fetch_top_memories(
        self, conn, user_id: int, limit: int, min_importance: int
    ) -> list[str]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT memory_text
                FROM memories
                WHERE user_id = %s AND importance >= %s
                ORDER BY importance DESC, created_at DESC
                LIMIT %s
                """,
                (user_id, min_importance, limit),
            )
            return [str(row[0]) for row in cur.fetchall()]

    def _fetch_latest_summary(self, conn, user_id: int) -> str | None:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT summary_text
                FROM summaries
                WHERE user_id = %s
                ORDER BY summary_date DESC
                LIMIT 1
                """,
                (user_id,),
            )
            row = cur.fetchone()
        return str(row[0]) if row else None

    @staticmethod
    def _format_prompt_block(
        *,
        name: str,
        recent: list[tuple[str, str]],
        memories: list[str],
        summary: str | None,
    ) -> str:
        parts: list[str] = [
            f"You are speaking directly to {name}. Always use second person (you/we). "
            f"Never refer to {name} in third person."
        ]

        if memories:
            lines = "\n".join(f"- {m}" for m in memories)
            parts.append(f"Known facts about them:\n{lines}")

        if summary:
            parts.append(f"Earlier session summary:\n{summary.strip()}")

        if recent:
            lines: list[str] = []
            for user_text, assistant_text in recent:
                lines.append(
                    f"- You asked: {truncate_context_text(user_text)} | "
                    f"I answered: {truncate_context_text(assistant_text)}"
                )
            parts.append(
                "Recent session history (may contain speech-to-text errors — ignore fragments):\n"
                + "\n".join(lines)
            )

        if len(parts) == 1:
            return ""

        parts.append(
            "If they ask what you just discussed, briefly recap 1–3 topics from the history "
            'using second person, e.g. "You asked about Mars and the full forms of CPU and GPU." '
            "Do not say their name as if talking about someone else. "
            "Do not invent topics not in the history."
        )
        return "\n\n".join(parts)

    def _log_conversation_safe(
        self, user_id: int, user_text: str, assistant_text: str
    ) -> None:
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO conversations (user_id, user_text, assistant_text)
                        VALUES (%s, %s, %s)
                        """,
                        (user_id, user_text.strip(), assistant_text.strip()),
                    )
                conn.commit()
            self.after_conversation_logged(user_id, user_text, assistant_text)
        except Exception as exc:
            logger.warning("Conversation log failed: %s", exc)
            self._last_error = str(exc)

    def _extract_memories_safe(
        self, user_id: int, user_text: str, assistant_text: str
    ) -> None:
        """Phase B — background memory extraction via Ollama."""
        try:
            from llm_service import DEFAULT_MODEL, DEFAULT_OLLAMA_URL, ollama_generate

            prompt = (
                "Extract useful long-term memories from this conversation.\n"
                "Return JSON only: a list of objects with keys memory (string) and "
                f"importance (integer 0-10). Only include importance >= {SETTINGS.min_importance}.\n\n"
                f"User:\n{user_text.strip()}\n\n"
                f"Assistant:\n{assistant_text.strip()}\n"
            )
            raw = ollama_generate(
                prompt,
                model=os.environ.get("OLLAMA_MODEL", DEFAULT_MODEL),
                api_url=os.environ.get("OLLAMA_URL", DEFAULT_OLLAMA_URL),
                num_predict=256,
                timeout_s=45,
            )
            items = _parse_memory_json(raw)
            if not items:
                return
            with self._connect() as conn:
                with conn.cursor() as cur:
                    for item in items:
                        imp = int(item.get("importance", 0))
                        text = str(item.get("memory", "")).strip()
                        if not text or imp < SETTINGS.min_importance:
                            continue
                        cur.execute(
                            """
                            INSERT INTO memories (user_id, memory_text, importance)
                            VALUES (%s, %s, %s)
                            """,
                            (user_id, text, imp),
                        )
                conn.commit()
            logger.info("Extracted %d memories for user_id=%s", len(items), user_id)
        except Exception as exc:
            logger.warning("Memory extraction failed: %s", exc)

    def _run_summary_catchup_safe(self) -> None:
        """Phase C — summarize yesterday for users with conversations."""
        if not self._ready:
            return
        try:
            target = date.today() - timedelta(days=1)
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT DISTINCT user_id FROM conversations
                        WHERE timestamp::date = %s
                        """,
                        (target,),
                    )
                    user_ids = [int(row[0]) for row in cur.fetchall()]
                for uid in user_ids:
                    self._summarize_user_day(conn, uid, target)
        except Exception as exc:
            logger.warning("Daily summary catchup failed: %s", exc)

    def _summarize_user_day(self, conn, user_id: int, day: date) -> None:
        from llm_service import DEFAULT_MODEL, DEFAULT_OLLAMA_URL, ollama_generate

        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM summaries WHERE user_id = %s AND summary_date = %s",
                (user_id, day),
            )
            if cur.fetchone():
                return
            cur.execute(
                """
                SELECT user_text, assistant_text
                FROM conversations
                WHERE user_id = %s AND timestamp::date = %s
                ORDER BY timestamp ASC
                """,
                (user_id, day),
            )
            rows = cur.fetchall()
            if not rows:
                return
            transcript = "\n".join(
                f"User: {u}\nAssistant: {a}" for u, a in rows
            )
            prompt = (
                f"Summarize this user's conversations on {day.isoformat()} in 2-4 short bullet topics.\n"
                "Plain text only, suitable to read aloud tomorrow.\n\n"
                f"{transcript}"
            )
            summary = ollama_generate(
                prompt,
                model=os.environ.get("OLLAMA_MODEL", DEFAULT_MODEL),
                api_url=os.environ.get("OLLAMA_URL", DEFAULT_OLLAMA_URL),
                num_predict=128,
                timeout_s=60,
            )
            cur.execute(
                """
                INSERT INTO summaries (user_id, summary_date, summary_text)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_id, summary_date) DO NOTHING
                """,
                (user_id, day, summary.strip()),
            )
        conn.commit()


def _parse_memory_json(raw: str) -> list[dict[str, Any]]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if not match:
            return []
        data = json.loads(match.group(0))
    if isinstance(data, dict):
        data = data.get("memories", [])
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def get_memory_service() -> MemoryService:
    global _service
    with _lock:
        if _service is None:
            _service = MemoryService()
        return _service
