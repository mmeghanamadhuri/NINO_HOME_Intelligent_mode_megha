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
from datetime import date, datetime, timedelta
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
        r"^\.\.\.\s*",
        r"^\*+\s*$",
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
    from memory_filters import filter_recent_turns_for_prompt

    return filter_recent_turns_for_prompt(
        [
            (user_text, assistant_text)
            for user_text, assistant_text in recent
            if not is_stt_fragment(user_text)
        ]
    )


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
    recent_turns: int = 10
    top_memories: int = 10
    min_importance: int = 5
    extraction_enabled: bool = False
    summary_cron_enabled: bool = False
    summary_scheduler_time: str = "00:05"


def parse_summary_scheduler_time(raw: str) -> tuple[int, int]:
    """Parse MEMORY_SUMMARY_CRON_TIME (HH:MM local) for the daily summary job."""
    text = (raw or "00:05").strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if not match:
        raise ValueError(f"Invalid MEMORY_SUMMARY_CRON_TIME: {raw!r} (expected HH:MM)")
    hour = int(match.group(1))
    minute = int(match.group(2))
    if hour > 23 or minute > 59:
        raise ValueError(f"Invalid MEMORY_SUMMARY_CRON_TIME: {raw!r}")
    return hour, minute


def seconds_until_local_time(
    hour: int, minute: int, *, now: datetime | None = None
) -> float:
    """Seconds until the next local clock occurrence of hour:minute."""
    current = now or datetime.now()
    target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= current:
        target += timedelta(days=1)
    return max(0.0, (target - current).total_seconds())


def yesterday_local_date(*, now: datetime | None = None) -> date:
    """Calendar yesterday in local PC time (matches alarm clock)."""
    return (now or datetime.now()).date() - timedelta(days=1)


SETTINGS = MemorySettings()
_lock = threading.Lock()
_service: MemoryService | None = None


def configure_from_environ() -> None:
    SETTINGS.database_url = normalize_database_url(os.environ.get("DATABASE_URL", ""))
    SETTINGS.recent_turns = max(1, int(os.environ.get("MEMORY_RECENT_TURNS", "10")))
    SETTINGS.top_memories = max(1, int(os.environ.get("MEMORY_TOP_MEMORIES", "10")))
    SETTINGS.min_importance = int(os.environ.get("MEMORY_MIN_IMPORTANCE", "5"))
    raw_extraction = os.environ.get("MEMORY_EXTRACTION")
    if raw_extraction is None and SETTINGS.database_url:
        # Phase B on by default when PostgreSQL memory is configured.
        SETTINGS.extraction_enabled = True
    else:
        SETTINGS.extraction_enabled = (raw_extraction or "0").strip().lower() in {
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
    SETTINGS.summary_scheduler_time = os.environ.get("MEMORY_SUMMARY_CRON_TIME", "00:05").strip()


class MemoryService:
    def __init__(self) -> None:
        self._ready = False
        self._last_error = ""
        self._schema_checked = False
        self._summary_stop = threading.Event()
        self._summary_scheduler_thread: threading.Thread | None = None

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
            if SETTINGS.extraction_enabled:
                logger.info(
                    "Memory Phase B enabled — long-term extraction after each logged turn "
                    "(importance >= %s)",
                    SETTINGS.min_importance,
                )
            if SETTINGS.summary_cron_enabled:
                threading.Thread(
                    target=self._run_summary_catchup_safe,
                    daemon=True,
                    name="memory-summary-catchup",
                ).start()
                self._summary_stop.clear()
                self._summary_scheduler_thread = threading.Thread(
                    target=self._run_summary_scheduler_loop,
                    daemon=True,
                    name="memory-summary-scheduler",
                )
                self._summary_scheduler_thread.start()
                logger.info(
                    "Memory Phase C scheduler started (daily at %s local)",
                    SETTINGS.summary_scheduler_time,
                )
        except Exception as exc:
            self._ready = False
            self._last_error = str(exc)
            logger.warning("Memory layer unavailable: %s", exc)

    def stop(self) -> None:
        self._summary_stop.set()

    def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "enabled": self.enabled,
            "ready": self._ready,
            "database_url_set": bool(SETTINGS.database_url),
            "recent_turns": SETTINGS.recent_turns,
            "top_memories": SETTINGS.top_memories,
            "min_importance": SETTINGS.min_importance,
            "extraction_enabled": SETTINGS.extraction_enabled,
            "summary_cron_enabled": SETTINGS.summary_cron_enabled,
            "summary_scheduler_time": SETTINGS.summary_scheduler_time,
            "summary_yesterday_date": yesterday_local_date().isoformat(),
            "last_error": self._last_error,
        }
        if self._ready and SETTINGS.summary_cron_enabled:
            try:
                hour, minute = parse_summary_scheduler_time(SETTINGS.summary_scheduler_time)
                out["summary_next_run_in_seconds"] = round(
                    seconds_until_local_time(hour, minute), 1
                )
            except ValueError as exc:
                out["summary_scheduler_error"] = str(exc)
        if self._ready:
            try:
                out["table_counts"] = self._fetch_table_counts()
            except Exception as exc:
                out["table_counts_error"] = str(exc)
        return out

    def table_stats(self) -> dict[str, Any]:
        """Row counts per memory table (for /api/memory/stats)."""
        if not self._ready:
            return {"ready": False, "last_error": self._last_error}
        return {"ready": True, "table_counts": self._fetch_table_counts()}

    def load_context(
        self, display_name: str | None, *, query_text: str = ""
    ) -> LoadedMemoryContext | None:
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
                summary = self._fetch_yesterday_summary(conn, user_id)
            recent = filter_recent_turns(recent)
            from memory_filters import filter_memories_for_query

            memories = filter_memories_for_query(memories, query_text)
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

    def get_latest_summary_text(self, display_name: str) -> str | None:
        """Phase C — calendar yesterday's summary for greeting + voice."""
        if not self._ready or not display_name:
            return None
        face_id = slug_face_id(display_name)
        if not face_id:
            return None
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT id FROM users
                        WHERE face_id = %s OR LOWER(name) = LOWER(%s)
                        ORDER BY CASE WHEN face_id = %s THEN 0 ELSE 1 END
                        LIMIT 1
                        """,
                        (face_id, display_name.strip(), face_id),
                    )
                    row = cur.fetchone()
                    if not row:
                        return None
                    user_id = int(row[0])
                return self._fetch_yesterday_summary(conn, user_id)
        except Exception as exc:
            logger.warning("get_latest_summary_text failed: %s", exc)
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

    def get_memory_text_by_key(self, user_id: int, memory_key: str) -> str | None:
        if not self._ready or not memory_key:
            return None
        from memory_filters import memory_key_lookup_candidates

        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    for key in memory_key_lookup_candidates(memory_key):
                        cur.execute(
                            """
                            SELECT memory_text FROM memories
                            WHERE user_id = %s AND memory_key = %s
                            ORDER BY updated_at DESC NULLS LAST, created_at DESC
                            LIMIT 1
                            """,
                            (user_id, key),
                        )
                        row = cur.fetchone()
                        if row:
                            return str(row[0]).strip()
            return None
        except Exception as exc:
            logger.warning("get_memory_text_by_key failed: %s", exc)
            return None

    def list_preference_memories(
        self, user_id: int, *, dislikes: bool = False
    ) -> list[str]:
        if not self._ready:
            return []
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    if dislikes:
                        cur.execute(
                            """
                            SELECT memory_text FROM memories
                            WHERE user_id = %s
                              AND (
                                memory_key LIKE 'dislike%%'
                                OR memory_text ILIKE '%%dislike%%'
                                OR memory_text ILIKE '%%don''t like%%'
                                OR memory_text ILIKE '%%hate%%'
                              )
                            ORDER BY updated_at DESC NULLS LAST, created_at DESC
                            """,
                            (user_id,),
                        )
                    else:
                        cur.execute(
                            """
                            SELECT memory_text FROM memories
                            WHERE user_id = %s
                              AND (
                                memory_key LIKE 'favorite_%%'
                                OR memory_key LIKE 'likes_%%'
                              )
                            ORDER BY updated_at DESC NULLS LAST, created_at DESC
                            """,
                            (user_id,),
                        )
                    rows = cur.fetchall()
            return [str(row[0]).strip() for row in rows if row and row[0]]
        except Exception as exc:
            logger.warning("list_preference_memories failed: %s", exc)
            return []

    def _facts_for_recall_key(self, user_id: int, key: str) -> list[str]:
        """Load memory rows for a recall slot (favorites, dislikes, birthdate, etc.)."""
        from memory_filters import RECALL_ALL_PREFERENCES, RECALL_DISLIKES

        if key == RECALL_ALL_PREFERENCES:
            return self.list_preference_memories(user_id, dislikes=False)
        if key == RECALL_DISLIKES:
            return self.list_preference_memories(user_id, dislikes=True)
        stored = self.get_memory_text_by_key(user_id, key)
        return [stored] if stored else []

    def answer_memory_recall(
        self,
        user_id: int,
        user_text: str,
        *,
        person_name: str = "",
        model: str | None = None,
        api_url: str | None = None,
    ) -> str | None:
        """Answer from the memories table — LLM phrases the reply from DB facts."""
        from llm_service import answer_memory_recall_reply
        from memory_filters import infer_recall_memory_key

        key = infer_recall_memory_key(user_text)
        if not key:
            return None
        facts = self._facts_for_recall_key(user_id, key)
        return answer_memory_recall_reply(
            user_text,
            facts,
            person_name=person_name,
            model=model,
            api_url=api_url,
        )

    def upsert_preference_from_utterance(
        self,
        user_id: int,
        user_text: str,
        *,
        person_name: str = "",
    ) -> str | None:
        """Immediately store a spoken preference update (before async extraction)."""
        from memory_filters import (
            normalize_memory_key,
            parse_birthdate_update,
            parse_like_dislike_update,
            parse_preference_update,
            preference_update_memory_key,
        )

        birthdate = parse_birthdate_update(user_text)
        if birthdate:
            memory_key = "birthdate"
            memory_text = f"My birthday is on {birthdate}"
        else:
            parsed = parse_preference_update(user_text)
            if parsed:
                topic, value = parsed
                memory_key = preference_update_memory_key(topic)
                memory_text = f"{value} is my favorite {topic}"
            else:
                like_dislike = parse_like_dislike_update(user_text)
                if not like_dislike:
                    return None
                kind, subject = like_dislike
                slug = normalize_memory_key(subject)[:48]
                if kind == "dislike":
                    memory_key = f"dislike_{slug}"
                    memory_text = f"I dislike {subject}"
                else:
                    memory_key = f"likes_{slug}"
                    memory_text = f"I like {subject}"
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    self._upsert_memory(cur, user_id, memory_text, 7, memory_key)
                conn.commit()
            logger.info(
                "Preference synced user_id=%s key=%s text=%r",
                user_id,
                memory_key,
                memory_text[:80],
            )
            return memory_key
        except Exception as exc:
            logger.warning("upsert_preference_from_utterance failed: %s", exc)
            return None

    def list_memory_keys_for_user(self, user_id: int) -> list[str]:
        if not self._ready:
            return []
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT DISTINCT memory_key FROM memories
                        WHERE user_id = %s AND memory_key IS NOT NULL
                        ORDER BY memory_key
                        """,
                        (user_id,),
                    )
                    rows = cur.fetchall()
            return [str(row[0]).strip() for row in rows if row and row[0]]
        except Exception as exc:
            logger.warning("list_memory_keys_for_user failed: %s", exc)
            return []

    def fetch_memories_for_keys(self, user_id: int, keys: list[str]) -> list[str]:
        from memory_filters import canonical_preference_key, memory_key_lookup_candidates

        if not self._ready:
            return []
        found: list[str] = []
        seen: set[str] = set()
        lookup_keys = keys or []
        if not lookup_keys:
            try:
                with self._connect() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            SELECT memory_text FROM memories
                            WHERE user_id = %s
                            ORDER BY importance DESC, updated_at DESC NULLS LAST
                            LIMIT 5
                            """,
                            (user_id,),
                        )
                        rows = cur.fetchall()
                return [str(r[0]).strip() for r in rows if r and r[0]]
            except Exception as exc:
                logger.warning("fetch_memories_for_keys fallback failed: %s", exc)
                return []

        for raw_key in lookup_keys:
            key = raw_key.strip()
            if not key:
                continue
            if not key.startswith("favorite_") and key not in {
                "birthdate",
                "hobbies",
                "job_title",
                "allergies",
                "location",
                "dislikes",
            }:
                key = canonical_preference_key(key)
            for candidate in memory_key_lookup_candidates(key):
                text = self.get_memory_text_by_key(user_id, candidate)
                if text and text.lower() not in seen:
                    seen.add(text.lower())
                    found.append(text)
        return found

    def store_llm_memory_items(
        self,
        user_id: int,
        items: list[dict[str, Any]],
        *,
        user_text: str,
        assistant_text: str = "",
    ) -> list[str]:
        from memory_filters import (
            enrich_llm_memory_text,
            infer_memory_key,
            is_valid_llm_memory_item,
        )

        if not self._ready or not items:
            return []
        stored: list[str] = []
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    for item in items:
                        raw_text = str(item.get("memory", "")).strip()
                        imp = int(item.get("importance", 7))
                        explicit_key = str(item.get("key", "") or "").strip()
                        if not raw_text or imp < SETTINGS.min_importance:
                            logger.info(
                                "LLM store skip (importance/text) user_id=%s text=%r",
                                user_id,
                                raw_text[:60],
                            )
                            continue
                        memory_key = infer_memory_key(raw_text, explicit_key)
                        text = enrich_llm_memory_text(raw_text, memory_key, user_text)
                        if not is_valid_llm_memory_item(
                            text, user_text=user_text, memory_key=memory_key
                        ):
                            logger.info(
                                "LLM store rejected user_id=%s key=%s text=%r",
                                user_id,
                                memory_key,
                                text[:80],
                            )
                            continue
                        self._upsert_memory(cur, user_id, text, imp, memory_key)
                        stored.append(text)
                conn.commit()
            if stored:
                logger.info(
                    "LLM memory store user_id=%s count=%d keys from %d items",
                    user_id,
                    len(stored),
                    len(items),
                )
            elif items:
                logger.warning(
                    "LLM memory store: all %d item(s) rejected for user_id=%s heard=%s",
                    len(items),
                    user_id,
                    user_text[:80],
                )
        except Exception as exc:
            logger.warning("store_llm_memory_items failed: %s", exc)
        return stored

    def extract_and_store_sync(
        self,
        user_id: int,
        user_text: str,
        *,
        assistant_text: str = "",
        model: str | None = None,
        api_url: str | None = None,
    ) -> list[str]:
        """Fallback: dedicated LLM extraction when classify/store parsing failed."""
        from llm_service import DEFAULT_MODEL, DEFAULT_OLLAMA_URL, ollama_generate
        from memory_filters import infer_memory_key, is_valid_llm_memory_item, enrich_llm_memory_text

        prompt = self._build_extraction_prompt(
            user_text,
            min_importance=SETTINGS.min_importance,
        )
        try:
            raw = ollama_generate(
                prompt,
                model=model or os.environ.get("OLLAMA_MODEL", DEFAULT_MODEL),
                api_url=api_url or os.environ.get("OLLAMA_URL", DEFAULT_OLLAMA_URL),
                num_predict=256,
                timeout_s=45,
                temperature=0.2,
            )
            items = _parse_memory_json(raw)
            if not items:
                return []
            normalized: list[dict[str, Any]] = []
            for item in items:
                key = str(item.get("key", "") or "").strip()
                mem = str(item.get("memory", item.get("text", ""))).strip()
                if not mem:
                    continue
                memory_key = infer_memory_key(mem, key)
                mem = enrich_llm_memory_text(mem, memory_key, user_text)
                if is_valid_llm_memory_item(mem, user_text=user_text, memory_key=memory_key):
                    normalized.append(
                        {
                            "key": memory_key,
                            "memory": mem,
                            "importance": int(item.get("importance", 7)),
                        }
                    )
            return self.store_llm_memory_items(
                user_id,
                normalized,
                user_text=user_text,
                assistant_text=assistant_text,
            )
        except Exception as exc:
            logger.warning("extract_and_store_sync failed: %s", exc)
            return []

    def handle_llm_memory_turn(
        self,
        user_id: int,
        user_text: str,
        *,
        person_name: str = "",
        model: str | None = None,
        api_url: str | None = None,
    ) -> tuple[str, str] | None:
        """Route personal memory: store likes/dislikes first, recall questions from DB, else LLM classify."""
        from llm_service import (
            DEFAULT_MODEL,
            DEFAULT_OLLAMA_URL,
            MemoryTurnDecision,
            analyze_memory_turn,
            answer_memory_recall_reply,
            answer_memory_store_ack,
            is_conversation_recap_question,
        )
        from memory_filters import (
            is_memory_recall_question,
            is_preference_update_statement,
            user_shares_personal_fact,
        )

        if is_conversation_recap_question(user_text):
            return None

        resolved_model = model or os.environ.get("OLLAMA_MODEL", DEFAULT_MODEL)
        resolved_api = api_url or os.environ.get("OLLAMA_URL", DEFAULT_OLLAMA_URL)

        # 1) User is stating a like/dislike/favorite/birthday → write memories table immediately.
        if is_preference_update_statement(user_text):
            memory_key = self.upsert_preference_from_utterance(
                user_id, user_text, person_name=person_name
            )
            if memory_key:
                stored_text = self.get_memory_text_by_key(user_id, memory_key)
                stored = [stored_text] if stored_text else []
                reply = answer_memory_store_ack(
                    user_text,
                    stored,
                    person_name=person_name,
                    model=resolved_model,
                    api_url=resolved_api,
                )
                return "memory_llm_store", reply

        # 2) User is asking about saved preferences → read DB, LLM speaks the answer.
        if is_memory_recall_question(user_text):
            recall_reply = self.answer_memory_recall(
                user_id,
                user_text,
                person_name=person_name,
                model=resolved_model,
                api_url=resolved_api,
            )
            if recall_reply:
                return "memory_llm_recall", recall_reply

        known_keys = self.list_memory_keys_for_user(user_id)
        decision = analyze_memory_turn(
            user_text,
            person_name=person_name,
            known_memory_keys=known_keys,
            model=resolved_model,
            api_url=resolved_api,
        )

        # Statements misclassified as recall (e.g. "my favorite food is lemon rice") → store.
        if decision.action == "recall" and (
            is_preference_update_statement(user_text)
            or (
                user_shares_personal_fact(user_text)
                and not is_memory_recall_question(user_text)
            )
        ):
            logger.info(
                "Memory override recall→store user_id=%s heard=%s",
                user_id,
                user_text[:80],
            )
            decision = MemoryTurnDecision(action="store")

        # STT fragments like "favorite" alone — not a real recall question.
        if decision.action == "recall" and not is_memory_recall_question(user_text):
            return None

        logger.info(
            "LLM memory decision user_id=%s action=%s recall_keys=%s store_items=%d heard=%s",
            user_id,
            decision.action,
            decision.recall_keys,
            len(decision.store),
            user_text[:80],
        )
        if decision.action == "recall":
            facts = self.fetch_memories_for_keys(user_id, decision.recall_keys)
            reply = answer_memory_recall_reply(
                user_text,
                facts,
                person_name=person_name,
                model=resolved_model,
                api_url=resolved_api,
            )
            return "memory_llm_recall", reply

        if decision.action == "store":
            stored = self.store_llm_memory_items(
                user_id, decision.store, user_text=user_text
            )
            if not stored:
                stored = self.extract_and_store_sync(
                    user_id,
                    user_text,
                    model=resolved_model,
                    api_url=resolved_api,
                )
            if not stored and is_preference_update_statement(user_text):
                memory_key = self.upsert_preference_from_utterance(
                    user_id, user_text, person_name=person_name
                )
                if memory_key:
                    stored_text = self.get_memory_text_by_key(user_id, memory_key)
                    if stored_text:
                        stored = [stored_text]
            if not stored:
                return None
            reply = answer_memory_store_ack(
                user_text,
                stored,
                person_name=person_name,
                model=resolved_model,
                api_url=resolved_api,
            )
            return "memory_llm_store", reply

        return None

    def log_conversation_background(
        self,
        user_id: int,
        user_text: str,
        assistant_text: str,
        *,
        reply_path: str = "llm",
    ) -> None:
        if not self._ready:
            return
        threading.Thread(
            target=self._log_conversation_safe,
            args=(user_id, user_text, assistant_text, reply_path),
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
        reply_path: str = "llm",
    ) -> str:
        """Log exchange; returns skip reason or 'queued'."""
        if not display_name:
            return "no_viewer"
        if not self._ready:
            return "db_not_ready"
        from memory_filters import conversation_log_skip_reason

        skip = conversation_log_skip_reason(user_text, reply_path=reply_path)
        if skip:
            logger.info("Skipping conversation log (%s): %s", skip, user_text[:80])
            return skip
        ctx = existing or self.ensure_user(display_name)
        if not ctx:
            return "user_resolve_failed"
        self.log_conversation_background(
            ctx.user_id, user_text, assistant_text, reply_path=reply_path
        )
        return "queued"

    def after_conversation_logged(
        self,
        user_id: int,
        user_text: str,
        assistant_text: str,
        *,
        reply_path: str = "llm",
    ) -> None:
        """Phase B hook — run after conversation INSERT (backup if sync LLM did not store)."""
        if not self._ready or not SETTINGS.extraction_enabled:
            return
        if reply_path in {
            "memory_llm_store",
            "memory_llm_recall",
            "memory_recall",
            "memory_update",
            "stt_rejected",
        }:
            return
        from memory_filters import should_extract_memories

        if not should_extract_memories(user_text, reply_path=reply_path):
            logger.info(
                "Memory extraction skipped for user_id=%s reply_path=%s heard=%s",
                user_id,
                reply_path,
                user_text[:80],
            )
            return
        threading.Thread(
            target=self._extract_memories_safe,
            args=(user_id, user_text, assistant_text),
            daemon=True,
            name="memory-extract",
        ).start()

    # ------------------------------------------------------------------ internals

    def _fetch_table_counts(self) -> dict[str, int]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                counts: dict[str, int] = {}
                for table in ("users", "conversations", "memories", "summaries", "alarms"):
                    cur.execute(f"SELECT COUNT(*) FROM {table}")
                    row = cur.fetchone()
                    counts[table] = int(row[0]) if row else 0
        return counts

    def _memory_already_stored(self, conn, user_id: int, memory_text: str) -> bool:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1 FROM memories
                WHERE user_id = %s AND LOWER(TRIM(memory_text)) = LOWER(TRIM(%s))
                LIMIT 1
                """,
                (user_id, memory_text),
            )
            return cur.fetchone() is not None

    @staticmethod
    def _build_extraction_prompt(user_text: str, *, min_importance: int) -> str:
        return (
            "Extract durable personal facts the USER stated about themselves.\n"
            "STORE only if the USER explicitly shared durable personal facts.\n"
            "Categories: preferences, likes/dislikes, hobbies, job, relationships, "
            "allergies, location, birthdate, anniversaries, family, pets, health, "
            "education, goals, nationality, languages, nickname, favorites "
            "(food, drink, sport, color, movie, music, game, book, show).\n"
            "SKIP: jokes, sarcasm, assistant summaries, trivia questions, greetings, "
            "thanks, transient moods, weather, meta-advice, things only the assistant said.\n"
            "Return JSON only: a list of objects with keys key (snake_case slot name), "
            "memory (short third-person fact), and importance (integer 0-10).\n"
            f"Only include importance >= {min_importance}.\n"
            "Use keys like birthdate, favorite_drink, favorite_sport, favorite_food, "
            "job_title, hobbies, allergies, location, education, pets, family, "
            "dislikes, goals, nickname, anniversary.\n"
            "If nothing durable was stated by the user, return [].\n\n"
            f"User said:\n{user_text.strip()}\n"
        )

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
        self._purge_junk_memories()

    def _purge_junk_memories(self) -> None:
        """Remove known bad rows (jokes, fragments, meta lines)."""
        from memory_filters import infer_memory_key, is_junk_memory_text

        junk_ids: list[int] = []
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT id, memory_text FROM memories")
                    rows = cur.fetchall()
                    junk_ids = [int(r[0]) for r in rows if is_junk_memory_text(str(r[1]))]
                    if junk_ids:
                        cur.execute("DELETE FROM memories WHERE id = ANY(%s)", (junk_ids,))
                    cur.execute(
                        """
                        DELETE FROM memories
                        WHERE memory_text ILIKE '%don''t believe everything%'
                           OR memory_text ILIKE '%not born on june%'
                           OR memory_text ILIKE '%just a joke%'
                           OR memory_text ILIKE 'currently working on projects%'
                           OR memory_text ILIKE '%[insert%'
                           OR memory_text ILIKE '%insert actual date%'
                           OR memory_text ILIKE '%are my hobbies%'
                           OR memory_text ILIKE '%preferred beverage%'
                           OR memory_text ILIKE '%reading and playing video games%'
                           OR memory_text ILIKE '%enjoy playing board games%'
                        """
                    )
                    cur.execute(
                        """
                        SELECT id, user_id, memory_text, importance
                        FROM memories
                        WHERE memory_key IS NULL
                        ORDER BY importance DESC, id DESC
                        """
                    )
                    for row_id, user_id, text, importance in cur.fetchall():
                        key = infer_memory_key(str(text))
                        cur.execute(
                            """
                            SELECT id, importance FROM memories
                            WHERE user_id = %s AND memory_key = %s AND id <> %s
                            LIMIT 1
                            """,
                            (user_id, key, int(row_id)),
                        )
                        existing = cur.fetchone()
                        if existing:
                            keep_new = int(importance) >= int(existing[1])
                            if keep_new:
                                cur.execute(
                                    "DELETE FROM memories WHERE id = %s",
                                    (int(existing[0]),),
                                )
                                cur.execute(
                                    """
                                    UPDATE memories
                                    SET memory_key = %s, updated_at = NOW()
                                    WHERE id = %s
                                    """,
                                    (key, int(row_id)),
                                )
                            else:
                                cur.execute(
                                    "DELETE FROM memories WHERE id = %s",
                                    (int(row_id),),
                                )
                        else:
                            cur.execute(
                                """
                                UPDATE memories
                                SET memory_key = %s, updated_at = NOW()
                                WHERE id = %s
                                """,
                                (key, int(row_id)),
                            )
                conn.commit()
            if junk_ids:
                logger.info("Memory hygiene removed %d junk row(s)", len(junk_ids))
        except Exception as exc:
            logger.warning("Memory hygiene skipped: %s", exc)

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

    def _fetch_yesterday_summary(self, conn, user_id: int) -> str | None:
        return self._fetch_summary_for_date(conn, user_id, yesterday_local_date())

    def _fetch_summary_for_date(self, conn, user_id: int, day: date) -> str | None:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT summary_text
                FROM summaries
                WHERE user_id = %s AND summary_date = %s
                """,
                (user_id, day),
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
            for user_text, _assistant_text in recent:
                cleaned_user = truncate_context_text(user_text)
                if cleaned_user:
                    lines.append(f"- User said: {cleaned_user}")
            if lines:
                parts.append(
                    "Recent things they asked or said (speech-to-text may have errors):\n"
                    + "\n".join(lines)
                )

        if len(parts) == 1:
            return ""

        parts.append(
            "Known facts about them are authoritative — if recent lines disagree, "
            "trust the known facts (especially for preferences and personal details). "
            "Use known facts and recent user lines only when they directly answer the "
            "current question. "
            "Answer in fresh, casual spoken wording every time — never copy a previous reply verbatim. "
            "If they ask the same thing again, change tone and phrasing while keeping the same facts. "
            "When several related facts exist (e.g. tea and coffee), combine them naturally. "
            "Do not use repetitive templates such as 'You asked about...'. "
            "Do not say their name as if talking about someone else. "
            "Do not invent topics not supported by the facts. "
            "Do not mention unrelated stored facts (birthdays, hobbies, jokes) unless the user asked about them."
        )
        return "\n\n".join(parts)

    def _log_conversation_safe(
        self,
        user_id: int,
        user_text: str,
        assistant_text: str,
        reply_path: str = "llm",
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
            self.after_conversation_logged(
                user_id, user_text, assistant_text, reply_path=reply_path
            )
        except Exception as exc:
            logger.warning("Conversation log failed: %s", exc)
            self._last_error = str(exc)

    def _upsert_memory(
        self, cur, user_id: int, memory_text: str, importance: int, memory_key: str
    ) -> None:
        cur.execute(
            "DELETE FROM memories WHERE user_id = %s AND memory_key = %s",
            (user_id, memory_key),
        )
        cur.execute(
            """
            INSERT INTO memories (user_id, memory_key, memory_text, importance, updated_at)
            VALUES (%s, %s, %s, %s, NOW())
            """,
            (user_id, memory_key, memory_text, importance),
        )

    def _extract_memories_safe(
        self, user_id: int, user_text: str, assistant_text: str
    ) -> None:
        """Phase B — background memory extraction via Ollama."""
        try:
            from llm_service import DEFAULT_MODEL, DEFAULT_OLLAMA_URL, ollama_generate
            from memory_filters import (
                infer_memory_key,
                is_valid_memory_text,
            )

            prompt = self._build_extraction_prompt(
                user_text,
                min_importance=SETTINGS.min_importance,
            )
            raw = ollama_generate(
                prompt,
                model=os.environ.get("OLLAMA_MODEL", DEFAULT_MODEL),
                api_url=os.environ.get("OLLAMA_URL", DEFAULT_OLLAMA_URL),
                num_predict=256,
                timeout_s=45,
                temperature=0.2,
            )
            items = _parse_memory_json(raw)
            if not items:
                logger.info("Memory extraction: no items parsed for user_id=%s", user_id)
                return
            inserted = 0
            with self._connect() as conn:
                with conn.cursor() as cur:
                    for item in items:
                        imp = int(item.get("importance", 0))
                        text = str(item.get("memory", "")).strip()
                        explicit_key = str(item.get("key", "") or "").strip()
                        if not text or imp < SETTINGS.min_importance:
                            continue
                        if not is_valid_memory_text(
                            text, user_text=user_text, assistant_text=assistant_text
                        ):
                            continue
                        memory_key = infer_memory_key(text, explicit_key)
                        self._upsert_memory(cur, user_id, text, imp, memory_key)
                        inserted += 1
                conn.commit()
            if inserted:
                logger.info(
                    "Memory Phase B stored %d fact(s) for user_id=%s (parsed %d)",
                    inserted,
                    user_id,
                    len(items),
                )
            else:
                logger.info(
                    "Memory Phase B: nothing stored for user_id=%s (parsed %d, below threshold or duplicate)",
                    user_id,
                    len(items),
                )
        except Exception as exc:
            logger.warning("Memory extraction failed: %s", exc)

    def _run_summary_scheduler_loop(self) -> None:
        """Phase C — run daily summary catch-up at MEMORY_SUMMARY_CRON_TIME local."""
        while not self._summary_stop.is_set():
            try:
                hour, minute = parse_summary_scheduler_time(SETTINGS.summary_scheduler_time)
                wait_s = seconds_until_local_time(hour, minute)
                logger.info(
                    "Next daily summary scheduled in %.0f s (at %s local)",
                    wait_s,
                    SETTINGS.summary_scheduler_time,
                )
                if self._summary_stop.wait(wait_s):
                    break
                logger.info("Running scheduled daily summary catch-up")
                self._run_summary_catchup_safe()
            except ValueError as exc:
                logger.warning("Summary scheduler disabled: %s", exc)
                return
            except Exception as exc:
                logger.warning("Summary scheduler error: %s", exc)
                if self._summary_stop.wait(60.0):
                    break

    def _run_summary_catchup_safe(self) -> None:
        """Phase C — summarize yesterday for users with conversations."""
        if not self._ready:
            return
        try:
            target = yesterday_local_date()
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
            logger.info(
                "Daily summary catch-up done for %s (%d user(s) with conversations)",
                target.isoformat(),
                len(user_ids),
            )
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


def resolve_alarm_user(
    camera_identity_name: str | None,
    camera_identity_state: str = "no_face",
) -> tuple[int | None, str]:
    """Return (user_id, person_name) for an alarm — user row only when face is recognized."""
    if camera_identity_state != "recognized":
        return None, ""
    if not camera_identity_name:
        return None, ""
    cleaned = str(camera_identity_name).strip()
    if not cleaned or cleaned.lower() in {"unknown", "face"}:
        return None, ""
    svc = get_memory_service()
    if not svc.ready:
        return None, cleaned[:64]
    ctx = svc.ensure_user(cleaned)
    if not ctx:
        return None, cleaned[:64]
    return ctx.user_id, cleaned[:64]


def get_memory_service() -> MemoryService:
    global _service
    with _lock:
        if _service is None:
            _service = MemoryService()
        return _service
