"""Persistent alarm scheduler — fires ESP /play_wav at scheduled times."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from alarm_time import system_clock_info, system_now, system_now_iso
from esp_playback import esp_play_wav_url, post_wav_to_esp
from tts_service import synthesize_sapi_wav_bytes
from wav_resample import ESP_PCM_SAMPLE_RATE_HZ, resample_wav_bytes_to_mono_16bit

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_ALARM_WAV = BASE_DIR.parent / "main" / "beep.wav"
DEFAULT_DATA_PATH = BASE_DIR / "data" / "alarms.json"

TICK_SECONDS = float(os.environ.get("ALARM_TICK_SECONDS", "1.0"))


@dataclass
class Alarm:
    id: str
    fire_at: str  # ISO 8601 local datetime
    label: str = ""
    person_name: str = ""  # from face recognition when alarm was set
    created_at: str = field(default_factory=system_now_iso)
    fired: bool = False

    def fire_datetime(self) -> datetime:
        return datetime.fromisoformat(self.fire_at)

    def spoken_time(self) -> str:
        dt = self.fire_datetime()
        hour = dt.hour % 12 or 12
        minute = dt.minute
        suffix = "AM" if dt.hour < 12 else "PM"
        if minute:
            return f"{hour}:{minute:02d} {suffix}"
        return f"{hour} {suffix}"

    def spoken_fire_message(self) -> str:
        """Spoken alert when the alarm fires on the ESP."""
        when = self.spoken_time()
        name = (self.person_name or "").strip()
        if self.label:
            label = self.label.strip()
            if name:
                return f"{name}, it's {when}, time for {label}."
            return f"It's {when}, time for {label}."
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

    def status(self) -> dict:
        with self._lock:
            pending = [a for a in self._alarms if not a.fired]
            return {
                "pending_count": len(pending),
                "pending": [
                    {
                        "id": a.id,
                        "fire_at": a.fire_at,
                        "label": a.label,
                        "person_name": a.person_name,
                        "spoken_time": a.spoken_time(),
                    }
                    for a in sorted(pending, key=lambda x: x.fire_at)
                ],
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
    ) -> Alarm:
        alarm = Alarm(
            id=uuid.uuid4().hex[:12],
            fire_at=fire_at.replace(second=0, microsecond=0).isoformat(timespec="seconds"),
            label=label.strip(),
            person_name=person_name.strip()[:64],
        )
        with self._lock:
            self._alarms.append(alarm)
            self._save_locked()
        logger.info(
            "Alarm set id=%s fire_at=%s label=%r person=%r",
            alarm.id,
            alarm.fire_at,
            alarm.label,
            alarm.person_name or "(none)",
        )
        return alarm

    def list_pending(self) -> list[Alarm]:
        with self._lock:
            return [a for a in self._alarms if not a.fired]

    def cancel_all(self) -> int:
        with self._lock:
            pending = [a for a in self._alarms if not a.fired]
            for alarm in pending:
                alarm.fired = True
            self._save_locked()
        logger.info("Cancelled %d alarm(s)", len(pending))
        return len(pending)

    def cancel_alarm(self, alarm_id: str) -> bool:
        with self._lock:
            for alarm in self._alarms:
                if alarm.id == alarm_id and not alarm.fired:
                    alarm.fired = True
                    self._save_locked()
                    logger.info("Cancelled alarm id=%s", alarm_id)
                    return True
        return False

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
                loaded.append(Alarm(**item))
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
        due: list[Alarm] = []
        with self._lock:
            for alarm in self._alarms:
                if alarm.fired:
                    continue
                if alarm.fire_datetime() <= now:
                    alarm.fired = True
                    due.append(alarm)
            if due:
                self._save_locked()

        for alarm in due:
            self._fire_alarm(alarm)

    def _fire_alarm(self, alarm: Alarm) -> None:
        logger.info("Alarm firing id=%s fire_at=%s", alarm.id, alarm.fire_at)
        try:
            if esp_play_wav_url() is None:
                raise RuntimeError("ESP_PLAY_WAV_URL is not set")

            spoken = alarm.spoken_fire_message()

            tts_wav, _ = synthesize_sapi_wav_bytes(spoken)
            tts_wav = resample_wav_bytes_to_mono_16bit(tts_wav, ESP_PCM_SAMPLE_RATE_HZ)
            post_wav_to_esp(tts_wav)

            alarm_wav = self._load_alarm_wav_bytes()
            if alarm_wav:
                post_wav_to_esp(alarm_wav)

            self._last_error = ""
        except Exception as exc:
            self._last_error = str(exc)
            logger.warning("Alarm fire failed id=%s: %s", alarm.id, exc)

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
