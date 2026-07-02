"""Persistent alarm scheduler — fires ESP /play_wav at scheduled times."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from alarm_medical import (
    ACK_AWAITING,
    ACK_CONFIRMED,
    ACK_NONE,
    ACK_RESCHEDULE_PROMPT,
    CATEGORY_GENERAL,
    CATEGORY_MEDICAL,
    PRIORITY_MEDICAL,
    PRIORITY_NORMAL,
    ack_prompt_suffix,
    classify_alarm_text,
    format_medical_fire_message,
    medical_repeat_minutes,
    normalize_label_for_user,
    repeat_prompt_suffix,
)
from alarm_time import system_clock_info, system_now, system_now_iso
from esp_playback import ESP_MAX_PLAY_WAV_BYTES, esp_play_wav_url, post_wav_to_esp
from memory_service import SETTINGS, get_memory_service
from tts_service import synthesize_sapi_wav_bytes
from wav_resample import ESP_PCM_SAMPLE_RATE_HZ, resample_wav_bytes_to_mono_16bit

try:
    import psycopg2
except ImportError:  # pragma: no cover
    psycopg2 = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_ALARM_WAV = BASE_DIR.parent / "main" / "beep.wav"
DEFAULT_DATA_PATH = BASE_DIR / "data" / "alarms.json"

TICK_SECONDS = float(os.environ.get("ALARM_TICK_SECONDS", "1.0"))
# 16 kHz keeps alarm TTS under ESP /play_wav limit (384 KiB); voice WS uses the same rate.
ALARM_TTS_SAMPLE_RATE_HZ = int(os.environ.get("ALARM_TTS_SAMPLE_RATE", "16000"))


@dataclass
class Alarm:
    id: str
    fire_at: str  # ISO 8601 local datetime
    label: str = ""
    person_name: str = ""  # from face recognition when alarm was set
    user_id: int | None = None
    created_at: str = field(default_factory=system_now_iso)
    fired: bool = False
    priority: int = PRIORITY_NORMAL  # 0 = medical P0, 1 = normal
    category: str = CATEGORY_GENERAL
    requires_ack: bool = False
    ack_state: str = ACK_NONE
    last_fired_at: str = ""
    next_repeat_at: str = ""

    def fire_datetime(self) -> datetime:
        return datetime.fromisoformat(self.fire_at)

    def is_medical(self) -> bool:
        return self.category == CATEGORY_MEDICAL or self.priority == PRIORITY_MEDICAL

    def is_scheduled(self) -> bool:
        return self.ack_state in ("", ACK_NONE) and not self.fired

    def spoken_time(self) -> str:
        dt = self.fire_datetime()
        hour = dt.hour % 12 or 12
        minute = dt.minute
        suffix = "AM" if dt.hour < 12 else "PM"
        if minute:
            return f"{hour}:{minute:02d} {suffix}"
        return f"{hour} {suffix}"

    def spoken_fire_message(self, *, repeat: bool = False) -> str:
        """Spoken alert when the alarm fires on the ESP."""
        when = self.spoken_time()
        name = (self.person_name or "").strip()
        if self.is_medical():
            return format_medical_fire_message(
                label=self.label,
                person_name=name,
                repeat=repeat,
            )

        if repeat and self.requires_ack:
            suffix = ""
        else:
            suffix = ack_prompt_suffix() if self.requires_ack else ""

        if self.label:
            label = normalize_label_for_user(self.label.strip())
            if name:
                core = f"{name}, it's {when}, time for {label}."
            else:
                core = f"It's {when}, time for {label}."
            return core + suffix

        if name:
            return f"{name}, alarm. It's {when}."
        return f"Alarm. It's {when}."


def _scope_matches(alarm: Alarm, user_id: int | None | object) -> bool:
    """Filter alarms by user scope. Ellipsis = no filter (web UI / scheduler)."""
    if user_id is ...:
        return True
    return alarm.user_id == user_id


def _parse_db_timestamp(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.replace(second=0, microsecond=0).isoformat(timespec="seconds")
    text = str(value).strip()
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text).replace(second=0, microsecond=0).isoformat(
            timespec="seconds"
        )
    except ValueError:
        return text


def _row_to_alarm(row: dict[str, Any]) -> Alarm:
    item = dict(row)
    item["fire_at"] = _parse_db_timestamp(item.get("fire_at"))
    item["created_at"] = _parse_db_timestamp(item.get("created_at")) or system_now_iso()
    item["last_fired_at"] = _parse_db_timestamp(item.get("last_fired_at"))
    item["next_repeat_at"] = _parse_db_timestamp(item.get("next_repeat_at"))
    item.setdefault("person_name", "")
    item.setdefault("user_id", None)
    item.setdefault("priority", PRIORITY_NORMAL)
    item.setdefault("category", CATEGORY_GENERAL)
    item.setdefault("requires_ack", False)
    item.setdefault("ack_state", ACK_NONE)
    item.setdefault("last_fired_at", "")
    item.setdefault("next_repeat_at", "")
    if not item.get("requires_ack"):
        prio, cat, req = classify_alarm_text(str(item.get("label", "")))
        if cat == CATEGORY_MEDICAL:
            item["priority"] = prio
            item["category"] = cat
            item["requires_ack"] = req
    return Alarm(**item)


class AlarmService:
    def __init__(
        self,
        data_path: Path | None = None,
        alarm_wav_path: Path | None = None,
    ) -> None:
        self._data_path = data_path or DEFAULT_DATA_PATH
        self._alarm_wav_path = alarm_wav_path or Path(
            os.environ.get("ALARM_WAV_PATH", str(DEFAULT_ALARM_WAV))
        )
        self._alarms: list[Alarm] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_error = ""
        self._active_ack_id: str | None = None
        self._reschedule_prompt_id: str | None = None
        self._db_ready = False

    @property
    def db_ready(self) -> bool:
        return self._db_ready

    def _connect(self):
        assert psycopg2 is not None
        return psycopg2.connect(SETTINGS.database_url)

    def _refresh_db_ready(self) -> None:
        self._db_ready = bool(
            SETTINGS.database_url and psycopg2 is not None and get_memory_service().ready
        )

    def start(self) -> None:
        self._refresh_db_ready()
        self._load()
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_loop, name="alarm-scheduler", daemon=True)
        self._thread.start()
        pending = len(self.list_pending())
        clock = system_clock_info()
        logger.info(
            "Alarm scheduler started (%d pending, system_now=%s %s, wav=%s, esp=%s)",
            pending,
            clock["now"],
            clock["timezone_name"],
            self._alarm_wav_path,
            esp_play_wav_url() or "(not set)",
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)

    def _alarm_public(self, alarm: Alarm) -> dict:
        return {
            "id": alarm.id,
            "fire_at": alarm.fire_at,
            "label": alarm.label,
            "person_name": alarm.person_name,
            "user_id": alarm.user_id,
            "spoken_time": alarm.spoken_time(),
            "priority": alarm.priority,
            "category": alarm.category,
            "requires_ack": alarm.requires_ack,
            "ack_state": alarm.ack_state,
            "next_repeat_at": alarm.next_repeat_at,
        }

    def status(self) -> dict:
        with self._lock:
            pending = [a for a in self._alarms if a.is_scheduled()]
            awaiting = [a for a in self._alarms if a.ack_state == ACK_AWAITING]
            return {
                "pending_count": len(pending),
                "awaiting_ack_count": len(awaiting),
                "pending": [
                    self._alarm_public(a)
                    for a in sorted(pending, key=lambda x: (x.priority, x.fire_at))
                ],
                "awaiting_ack": [self._alarm_public(a) for a in awaiting],
                "clock": system_clock_info(),
                "alarm_wav_path": str(self._alarm_wav_path),
                "alarm_wav_exists": self._alarm_wav_path.is_file(),
                "esp_play_wav_url": esp_play_wav_url() or "",
                "last_error": self._last_error,
                "storage": "postgresql" if self._db_ready else "json",
            }

    def add_alarm(
        self,
        fire_at: datetime,
        *,
        label: str = "",
        person_name: str = "",
        user_id: int | None = None,
        source_text: str = "",
        force_medical: bool = False,
    ) -> Alarm:
        priority, category, requires_ack = classify_alarm_text(label, source_text)
        if force_medical:
            priority, category, requires_ack = PRIORITY_MEDICAL, CATEGORY_MEDICAL, True

        alarm = Alarm(
            id=uuid.uuid4().hex[:12],
            fire_at=fire_at.replace(second=0, microsecond=0).isoformat(timespec="seconds"),
            label=label.strip(),
            person_name=person_name.strip()[:64],
            user_id=user_id,
            priority=priority,
            category=category,
            requires_ack=requires_ack,
        )
        with self._lock:
            self._alarms.append(alarm)
            self._persist_alarm_locked(alarm)
        logger.info(
            "Alarm set id=%s fire_at=%s label=%r person=%r user_id=%s priority=%s category=%s ack=%s",
            alarm.id,
            alarm.fire_at,
            alarm.label,
            alarm.person_name or "(none)",
            alarm.user_id if alarm.user_id is not None else "(anonymous)",
            alarm.priority,
            alarm.category,
            alarm.requires_ack,
        )
        return alarm

    def list_pending(self, user_id: int | None | object = ...) -> list[Alarm]:
        with self._lock:
            return [
                a
                for a in self._alarms
                if a.is_scheduled() and _scope_matches(a, user_id)
            ]

    def list_awaiting_ack(self, user_id: int | None | object = ...) -> list[Alarm]:
        with self._lock:
            return [
                a
                for a in self._alarms
                if a.ack_state == ACK_AWAITING and _scope_matches(a, user_id)
            ]

    def get_alarm(self, alarm_id: str) -> Alarm | None:
        with self._lock:
            for alarm in self._alarms:
                if alarm.id == alarm_id:
                    return alarm
        return None

    def get_active_ack_alarm(self) -> Alarm | None:
        with self._lock:
            if self._active_ack_id:
                for alarm in self._alarms:
                    if alarm.id == self._active_ack_id:
                        return alarm
            awaiting = [a for a in self._alarms if a.ack_state == ACK_AWAITING]
            if awaiting:
                return max(awaiting, key=lambda a: a.last_fired_at or "")
        return None

    def get_reschedule_prompt_alarm(self) -> Alarm | None:
        with self._lock:
            if not self._reschedule_prompt_id:
                return None
            for alarm in self._alarms:
                if alarm.id == self._reschedule_prompt_id:
                    return alarm
        return None

    def clear_reschedule_prompt(self) -> None:
        with self._lock:
            self._reschedule_prompt_id = None
            self._persist_runtime_state_locked()

    def confirm_ack(self, alarm_id: str) -> bool:
        with self._lock:
            for index, alarm in enumerate(self._alarms):
                if alarm.id != alarm_id:
                    continue
                if alarm.ack_state not in (ACK_AWAITING, ACK_RESCHEDULE_PROMPT):
                    return False
                del self._alarms[index]
                if self._active_ack_id == alarm_id:
                    self._active_ack_id = None
                if self._reschedule_prompt_id == alarm_id:
                    self._reschedule_prompt_id = None
                self._delete_alarm_locked(alarm_id)
                logger.info("Medical alarm ack confirmed id=%s", alarm_id)
                return True
        return False

    def decline_ack(self, alarm_id: str) -> bool:
        with self._lock:
            for alarm in self._alarms:
                if alarm.id != alarm_id:
                    continue
                if alarm.ack_state != ACK_AWAITING:
                    return False
                alarm.ack_state = ACK_RESCHEDULE_PROMPT
                alarm.next_repeat_at = ""
                self._reschedule_prompt_id = alarm_id
                self._active_ack_id = alarm_id
                self._persist_alarm_locked(alarm)
                logger.info("Medical alarm negative ack id=%s", alarm_id)
                return True
        return False

    def reschedule_alarm(self, alarm_id: str, fire_at: datetime) -> Alarm | None:
        with self._lock:
            for alarm in self._alarms:
                if alarm.id != alarm_id:
                    continue
                alarm.fire_at = fire_at.replace(second=0, microsecond=0).isoformat(
                    timespec="seconds"
                )
                alarm.ack_state = ACK_NONE
                alarm.fired = False
                alarm.last_fired_at = ""
                alarm.next_repeat_at = ""
                self._reschedule_prompt_id = None
                self._active_ack_id = None
                self._persist_alarm_locked(alarm)
                logger.info("Medical alarm rescheduled id=%s fire_at=%s", alarm_id, alarm.fire_at)
                return alarm
        return None

    def cancel_all(self, user_id: int | None | object = ...) -> int:
        """Remove scheduled and awaiting-ack alarms (optionally scoped to one user)."""
        with self._lock:
            before = len(self._alarms)
            removed_ids = [a.id for a in self._alarms if _scope_matches(a, user_id)]
            self._alarms = [a for a in self._alarms if not _scope_matches(a, user_id)]
            removed = before - len(self._alarms)
            if user_id is ...:
                self._active_ack_id = None
                self._reschedule_prompt_id = None
            else:
                if self._active_ack_id in removed_ids:
                    self._active_ack_id = None
                if self._reschedule_prompt_id in removed_ids:
                    self._reschedule_prompt_id = None
            self._delete_alarms_locked(removed_ids)
        logger.info("Deleted %d alarm(s) user_scope=%s", removed, user_id)
        return removed

    def cancel_alarm(self, alarm_id: str) -> bool:
        """Remove one alarm by id (pending or already fired)."""
        with self._lock:
            for index, alarm in enumerate(self._alarms):
                if alarm.id == alarm_id:
                    del self._alarms[index]
                    self._delete_alarm_locked(alarm_id)
                    logger.info("Deleted alarm id=%s", alarm_id)
                    return True
        return False

    def remove_pending_matching(
        self,
        *,
        fire_at: datetime | None = None,
        label_hint: str = "",
        user_id: int | None | object = ...,
    ) -> Alarm | None:
        """Delete a single pending alarm by time and/or label text."""
        with self._lock:
            pending = [
                a
                for a in self._alarms
                if a.is_scheduled() and _scope_matches(a, user_id)
            ]
            match = self._pick_pending_match(pending, fire_at=fire_at, label_hint=label_hint)
            if match is None:
                return None
            self._alarms = [a for a in self._alarms if a.id != match.id]
            self._delete_alarm_locked(match.id)
        logger.info("Deleted alarm id=%s via voice match", match.id)
        return match

    @staticmethod
    def _pick_pending_match(
        pending: list[Alarm],
        *,
        fire_at: datetime | None,
        label_hint: str,
    ) -> Alarm | None:
        if not pending:
            return None

        hint = label_hint.strip().lower()
        if hint:
            label_hits = [
                a
                for a in pending
                if hint in (a.label or "").lower() or (a.label or "").lower() in hint
            ]
            if len(label_hits) == 1:
                return label_hits[0]

        if fire_at is not None:
            time_hits: list[Alarm] = []
            for alarm in pending:
                delta = abs((alarm.fire_datetime() - fire_at).total_seconds())
                if delta <= 90:
                    time_hits.append(alarm)
            if len(time_hits) == 1:
                return time_hits[0]
            if len(time_hits) > 1 and hint:
                narrowed = [
                    a
                    for a in time_hits
                    if hint in (a.label or "").lower() or (a.label or "").lower() in hint
                ]
                if len(narrowed) == 1:
                    return narrowed[0]

        return None

    def _load(self) -> None:
        self._refresh_db_ready()
        if self._db_ready:
            self._alarms = self._load_from_db()
            self._import_json_to_db_if_needed()
            return
        self._alarms = self._load_from_json()

    def _load_from_db(self) -> list[Alarm]:
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT id, user_id, fire_at, label, person_name, created_at,
                               fired, priority, category, requires_ack, ack_state,
                               last_fired_at, next_repeat_at
                        FROM alarms
                        ORDER BY fire_at ASC
                        """
                    )
                    columns = [desc[0] for desc in cur.description]
                    rows = [dict(zip(columns, row)) for row in cur.fetchall()]
            loaded: list[Alarm] = []
            for row in rows:
                alarm = _row_to_alarm(row)
                if alarm.fired and alarm.ack_state == ACK_NONE:
                    continue
                loaded.append(alarm)
            logger.info("Loaded %d alarm(s) from PostgreSQL", len(loaded))
            return loaded
        except Exception as exc:
            logger.warning("Could not load alarms from PostgreSQL: %s", exc)
            self._last_error = str(exc)
            return []

    def _load_from_json(self) -> list[Alarm]:
        if not self._data_path.exists():
            return []
        try:
            raw = json.loads(self._data_path.read_text(encoding="utf-8"))
            items = raw if isinstance(raw, list) else raw.get("alarms", [])
            loaded: list[Alarm] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                item.setdefault("person_name", "")
                item.setdefault("user_id", None)
                item.setdefault("priority", PRIORITY_NORMAL)
                item.setdefault("category", CATEGORY_GENERAL)
                item.setdefault("requires_ack", False)
                item.setdefault("ack_state", ACK_NONE)
                item.setdefault("last_fired_at", "")
                item.setdefault("next_repeat_at", "")
                if not item.get("requires_ack"):
                    prio, cat, req = classify_alarm_text(str(item.get("label", "")))
                    if cat == CATEGORY_MEDICAL:
                        item["priority"] = prio
                        item["category"] = cat
                        item["requires_ack"] = req
                alarm = Alarm(**item)
                if alarm.fired and alarm.ack_state == ACK_NONE:
                    continue
                loaded.append(alarm)
            logger.info("Loaded %d alarm(s) from %s", len(loaded), self._data_path)
            return loaded
        except Exception as exc:
            logger.warning("Could not load alarms from %s: %s", self._data_path, exc)
            return []

    def _import_json_to_db_if_needed(self) -> None:
        if not self._data_path.is_file():
            return
        try:
            raw = json.loads(self._data_path.read_text(encoding="utf-8"))
            items = raw if isinstance(raw, list) else raw.get("alarms", [])
            if not items:
                return
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) FROM alarms")
                    count = int(cur.fetchone()[0])
                    if count > 0:
                        return
            json_alarms = self._load_from_json()
            if not json_alarms:
                return
            with self._lock:
                for alarm in json_alarms:
                    if any(a.id == alarm.id for a in self._alarms):
                        continue
                    self._alarms.append(alarm)
                    self._persist_alarm_locked(alarm)
            logger.info("Migrated %d alarm(s) from JSON to PostgreSQL", len(json_alarms))
        except Exception as exc:
            logger.warning("JSON alarm migration skipped: %s", exc)

    def _persist_alarm_locked(self, alarm: Alarm) -> None:
        if self._db_ready:
            self._upsert_alarm_db(alarm)
            return
        self._save_json_locked()

    def _delete_alarm_locked(self, alarm_id: str) -> None:
        if self._db_ready:
            self._delete_alarm_db(alarm_id)
            return
        self._save_json_locked()

    def _delete_alarms_locked(self, alarm_ids: list[str]) -> None:
        if not alarm_ids:
            return
        if self._db_ready:
            self._delete_alarms_db(alarm_ids)
            return
        self._save_json_locked()

    def _persist_runtime_state_locked(self) -> None:
        if self._db_ready:
            return
        self._save_json_locked()

    def _upsert_alarm_db(self, alarm: Alarm) -> None:
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO alarms (
                            id, user_id, fire_at, label, person_name, created_at,
                            fired, priority, category, requires_ack, ack_state,
                            last_fired_at, next_repeat_at
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s,
                            %s, %s
                        )
                        ON CONFLICT (id) DO UPDATE SET
                            user_id = EXCLUDED.user_id,
                            fire_at = EXCLUDED.fire_at,
                            label = EXCLUDED.label,
                            person_name = EXCLUDED.person_name,
                            fired = EXCLUDED.fired,
                            priority = EXCLUDED.priority,
                            category = EXCLUDED.category,
                            requires_ack = EXCLUDED.requires_ack,
                            ack_state = EXCLUDED.ack_state,
                            last_fired_at = EXCLUDED.last_fired_at,
                            next_repeat_at = EXCLUDED.next_repeat_at
                        """,
                        (
                            alarm.id,
                            alarm.user_id,
                            alarm.fire_at,
                            alarm.label,
                            alarm.person_name,
                            alarm.created_at or system_now_iso(),
                            alarm.fired,
                            alarm.priority,
                            alarm.category,
                            alarm.requires_ack,
                            alarm.ack_state or ACK_NONE,
                            alarm.last_fired_at or None,
                            alarm.next_repeat_at or None,
                        ),
                    )
                conn.commit()
        except Exception as exc:
            self._last_error = str(exc)
            logger.warning("Alarm DB upsert failed id=%s: %s", alarm.id, exc)

    def _delete_alarm_db(self, alarm_id: str) -> None:
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM alarms WHERE id = %s", (alarm_id,))
                conn.commit()
        except Exception as exc:
            self._last_error = str(exc)
            logger.warning("Alarm DB delete failed id=%s: %s", alarm_id, exc)

    def _delete_alarms_db(self, alarm_ids: list[str]) -> None:
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM alarms WHERE id = ANY(%s)", (alarm_ids,))
                conn.commit()
        except Exception as exc:
            self._last_error = str(exc)
            logger.warning("Alarm DB bulk delete failed: %s", exc)

    def _save_json_locked(self) -> None:
        self._data_path.parent.mkdir(parents=True, exist_ok=True)
        payload = [asdict(a) for a in self._alarms]
        self._data_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._check_due_alarms()
            except Exception:
                logger.exception("Alarm scheduler tick failed")
            self._stop.wait(TICK_SECONDS)

    def _check_due_alarms(self) -> None:
        now = system_now()
        due: list[tuple[Alarm, bool]] = []
        with self._lock:
            for alarm in self._alarms:
                if alarm.ack_state == ACK_RESCHEDULE_PROMPT:
                    continue
                if alarm.requires_ack:
                    if alarm.ack_state == ACK_AWAITING:
                        if not alarm.next_repeat_at:
                            continue
                        if datetime.fromisoformat(alarm.next_repeat_at) > now:
                            continue
                        due.append((alarm, True))
                        continue
                    if alarm.is_scheduled() and alarm.fire_datetime() <= now:
                        due.append((alarm, False))
                    elif (
                        alarm.ack_state == ACK_AWAITING
                        and alarm.last_fired_at
                        and not alarm.next_repeat_at
                    ):
                        # Failed fire left awaiting without repeat schedule — retry on interval
                        due.append((alarm, True))
                    continue
                if alarm.is_scheduled() and alarm.fire_datetime() <= now:
                    alarm.fired = True
                    due.append((alarm, False))
            if due:
                for alarm, is_repeat in due:
                    self._persist_alarm_locked(alarm)

        due.sort(key=lambda item: (item[0].priority, item[0].fire_at))
        for alarm, is_repeat in due:
            self._fire_alarm(alarm, repeat=is_repeat)

    def _mark_medical_awaiting_ack(self, alarm: Alarm, now: datetime) -> None:
        repeat_at = now + timedelta(minutes=medical_repeat_minutes())
        with self._lock:
            for stored in self._alarms:
                if stored.id != alarm.id:
                    continue
                stored.ack_state = ACK_AWAITING
                stored.last_fired_at = now.isoformat(timespec="seconds")
                stored.next_repeat_at = repeat_at.isoformat(timespec="seconds")
                self._active_ack_id = stored.id
                self._persist_alarm_locked(stored)
                break

    def _synthesize_alarm_wav_for_esp(self, spoken: str) -> bytes:
        """TTS for /play_wav — must fit ESP_MAX_PLAY_WAV_BYTES (384 KiB on device)."""
        for rate in (168, 200, 220):
            wav, _ = synthesize_sapi_wav_bytes(spoken, rate=rate)
            out = resample_wav_bytes_to_mono_16bit(wav, ALARM_TTS_SAMPLE_RATE_HZ)
            if len(out) <= ESP_MAX_PLAY_WAV_BYTES:
                return out

        trimmed = spoken.strip()
        if len(trimmed) > 96:
            trimmed = trimmed[:93].rstrip(" ,.") + "."
        wav, _ = synthesize_sapi_wav_bytes(trimmed, rate=220)
        out = resample_wav_bytes_to_mono_16bit(wav, ALARM_TTS_SAMPLE_RATE_HZ)
        if len(out) > ESP_MAX_PLAY_WAV_BYTES:
            raise RuntimeError(
                f"WAV too large for ESP ({len(out)} bytes; max {ESP_MAX_PLAY_WAV_BYTES})"
            )
        logger.warning("Alarm TTS trimmed to fit ESP: %r", trimmed)
        return out

    def _fire_alarm(self, alarm: Alarm, *, repeat: bool = False) -> None:
        logger.info(
            "Alarm firing id=%s fire_at=%s priority=%s category=%s repeat=%s",
            alarm.id,
            alarm.fire_at,
            alarm.priority,
            alarm.category,
            repeat,
        )
        now = system_now()
        try:
            if esp_play_wav_url() is None:
                raise RuntimeError("ESP_PLAY_WAV_URL is not set")

            spoken = alarm.spoken_fire_message(repeat=repeat)
            tts_wav = self._synthesize_alarm_wav_for_esp(spoken)
            post_wav_to_esp(tts_wav, prompt_ack=alarm.requires_ack)

            # Medical: TTS only (two long WAVs often exceed ESP limit). Normal: TTS + beep.
            if not alarm.requires_ack:
                alarm_wav = self._load_alarm_wav_bytes()
                if alarm_wav:
                    post_wav_to_esp(alarm_wav)

            if alarm.requires_ack:
                self._mark_medical_awaiting_ack(alarm, now)

            self._last_error = ""
        except Exception as exc:
            self._last_error = str(exc)
            logger.warning("Alarm fire failed id=%s: %s", alarm.id, exc)
            if alarm.requires_ack:
                self._mark_medical_awaiting_ack(alarm, now)

    def _load_alarm_wav_bytes(self) -> bytes | None:
        path = self._alarm_wav_path
        if not path.is_file():
            logger.warning("Alarm WAV not found at %s", path)
            return None
        raw = path.read_bytes()
        return resample_wav_bytes_to_mono_16bit(raw, ESP_PCM_SAMPLE_RATE_HZ)


_SERVICE: AlarmService | None = None


def get_alarm_service() -> AlarmService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = AlarmService()
    return _SERVICE
