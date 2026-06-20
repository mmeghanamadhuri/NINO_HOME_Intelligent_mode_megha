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
    medical_repeat_minutes,
    repeat_prompt_suffix,
)
from alarm_time import system_clock_info, system_now, system_now_iso
from esp_playback import ESP_MAX_PLAY_WAV_BYTES, esp_play_wav_url, post_wav_to_esp
from tts_service import synthesize_sapi_wav_bytes
from wav_resample import ESP_PCM_SAMPLE_RATE_HZ, resample_wav_bytes_to_mono_16bit

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
        if repeat and self.requires_ack:
            if self.is_medical():
                if name:
                    return f"{name}, medication reminder. Yes or no?"
                return "Medication reminder. Yes or no?"
            suffix = ""
        else:
            suffix = ack_prompt_suffix() if self.requires_ack else ""

        if self.label:
            from alarm_voice import normalize_label_for_user

            label = normalize_label_for_user(self.label.strip())
            if self.is_medical():
                core = (
                    f"{name}, medication at {when}: {label}."
                    if name
                    else f"Medication at {when}: {label}."
                )
            elif name:
                core = f"{name}, it's {when}, time for {label}."
            else:
                core = f"It's {when}, time for {label}."
            return core + suffix

        if self.is_medical():
            core = (
                f"{name}, medication at {when}."
                if name
                else f"Medication at {when}."
            )
            return core + suffix
        if name:
            return f"{name}, alarm. It's {when}."
        return f"Alarm. It's {when}."


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

    def start(self) -> None:
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
            }

    def add_alarm(
        self,
        fire_at: datetime,
        *,
        label: str = "",
        person_name: str = "",
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
            priority=priority,
            category=category,
            requires_ack=requires_ack,
        )
        with self._lock:
            self._alarms.append(alarm)
            self._save_locked()
        logger.info(
            "Alarm set id=%s fire_at=%s label=%r person=%r priority=%s category=%s ack=%s",
            alarm.id,
            alarm.fire_at,
            alarm.label,
            alarm.person_name or "(none)",
            alarm.priority,
            alarm.category,
            alarm.requires_ack,
        )
        return alarm

    def list_pending(self) -> list[Alarm]:
        with self._lock:
            return [a for a in self._alarms if a.is_scheduled()]

    def list_awaiting_ack(self) -> list[Alarm]:
        with self._lock:
            return [a for a in self._alarms if a.ack_state == ACK_AWAITING]

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
            self._save_locked()

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
                self._save_locked()
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
                self._save_locked()
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
                self._save_locked()
                logger.info("Medical alarm rescheduled id=%s fire_at=%s", alarm_id, alarm.fire_at)
                return alarm
        return None

    def cancel_all(self) -> int:
        """Remove all scheduled and awaiting-ack alarms."""
        with self._lock:
            before = len(self._alarms)
            self._alarms = []
            removed = before
            self._active_ack_id = None
            self._reschedule_prompt_id = None
            self._save_locked()
        logger.info("Deleted %d alarm(s)", removed)
        return removed

    def cancel_alarm(self, alarm_id: str) -> bool:
        """Remove one alarm by id (pending or already fired)."""
        with self._lock:
            for index, alarm in enumerate(self._alarms):
                if alarm.id == alarm_id:
                    del self._alarms[index]
                    self._save_locked()
                    logger.info("Deleted alarm id=%s", alarm_id)
                    return True
        return False

    def remove_pending_matching(
        self, *, fire_at: datetime | None = None, label_hint: str = ""
    ) -> Alarm | None:
        """Delete a single pending alarm by time and/or label text."""
        with self._lock:
            pending = [a for a in self._alarms if a.is_scheduled()]
            match = self._pick_pending_match(pending, fire_at=fire_at, label_hint=label_hint)
            if match is None:
                return None
            self._alarms = [a for a in self._alarms if a.id != match.id]
            self._save_locked()
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
        if not self._data_path.exists():
            self._alarms = []
            return
        try:
            raw = json.loads(self._data_path.read_text(encoding="utf-8"))
            items = raw if isinstance(raw, list) else raw.get("alarms", [])
            loaded: list[Alarm] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                item.setdefault("person_name", "")
                item.setdefault("priority", PRIORITY_NORMAL)
                item.setdefault("category", CATEGORY_GENERAL)
                item.setdefault("requires_ack", False)
                item.setdefault("ack_state", ACK_NONE)
                item.setdefault("last_fired_at", "")
                item.setdefault("next_repeat_at", "")
                if not item.get("requires_ack"):
                    prio, cat, req = classify_alarm_text(
                        str(item.get("label", "")),
                    )
                    if cat == CATEGORY_MEDICAL:
                        item["priority"] = prio
                        item["category"] = cat
                        item["requires_ack"] = req
                alarm = Alarm(**item)
                if alarm.fired and alarm.ack_state == ACK_NONE:
                    continue
                loaded.append(alarm)
            self._alarms = loaded
        except Exception as exc:
            logger.warning("Could not load alarms from %s: %s", self._data_path, exc)
            self._alarms = []

    def _save_locked(self) -> None:
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
                self._save_locked()

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
                self._save_locked()
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
