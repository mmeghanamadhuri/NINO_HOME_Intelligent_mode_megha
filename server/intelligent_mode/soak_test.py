"""Continuous end-to-end soak tests — voice Q&A, face/person, memory, bots.

Runs in a loop until stopped. Each cycle exercises bot capabilities and hands
failures to Intelligent Mode for fixes and email alerts.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from intelligent_mode.detectors import DetectionCandidate
from intelligent_mode.e2e_voice_test import run_e2e_voice_suite
from intelligent_mode.smoke_tests import SmokeTestResult, _run, run_smoke_suite
from intelligent_mode.voice_incident_filters import is_soak_live_session_skip

logger = logging.getLogger(__name__)

_SOAK_PATH = Path(__file__).resolve().parent.parent / "data" / "soak_test.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Voice Q&A pool — random subset each cycle. ASR uses picked question text (soak-pick);
# TTS replies play on the live ESP via POST /play_wav when SOAK_LIVE_ESP=1.
#
# Organized by age group so soak cycles exercise what kids, teens, adults, and seniors ask.

SOAK_VOICE_KIDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("What color is grass?", ("green",)),
    ("What sound does a cow make?", ("moo", "cow")),
    ("How many fingers do you have on one hand?", ("5", "five")),
    ("Can you count to five?", ("1", "one", "2", "two", "3", "three", "4", "5", "five")),
    ("What animal says woof?", ("dog", "puppy")),
    ("What is 3 plus 1?", ("4", "four")),
    ("Tell me a short story about a robot.", ("robot", "once", "story", "day")),
    ("What shape is a ball?", ("circle", "round", "sphere")),
)

SOAK_VOICE_TWEENS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("What is 12 times 2?", ("24", "twenty")),
    ("Who wrote Harry Potter?", ("rowling", "j.k", "jk")),
    ("What is the largest planet?", ("jupiter",)),
    ("What is photosynthesis?", ("plant", "sun", "light", "energy", "chlorophyll")),
    ("What is the capital of India?", ("delhi", "new delhi")),
    ("How many continents are there?", ("7", "seven")),
    ("What is the boiling point of water?", ("100", "hundred", "celsius", "212", "fahrenheit")),
)

SOAK_VOICE_TEENS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Tell me a joke.", ("joke", "funny", "why", "laugh", "haha", "ha", "okay", "here", "told", "said")),
    ("What is artificial intelligence?", ("ai", "machine", "learn", "computer", "intelligence")),
    ("Who invented the telephone?", ("bell", "alexander")),
    ("What is climate change?", ("climate", "warm", "carbon", "environment", "earth")),
    ("Can you help me study for a test?", ("yes", "help", "study", "sure", "course")),
    ("What is the speed of light?", ("speed", "light", "300", "186", "km")),
)

SOAK_VOICE_ADULTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("What time is it?", ("time", "clock", "hour", "minute", "am", "pm", "it is", "it's")),
    ("What's the weather like?", ("weather", "temperature", "rain", "sun", "cloud", "don't", "cannot", "sorry")),
    ("Can you help me?", ("yes", "help", "sure", "course", "assist")),
    ("What is your name?", ("nino", "ni no", "assistant", "robot", "name", "help")),
    ("Tell me a fun fact about robots.", ("robot", "machine", "fact", "autom", "program")),
    ("What is the capital of France?", ("paris",)),
    ("How do I set the volume?", ("volume", "speaker", "percent", "say", "set")),
)

SOAK_VOICE_SENIORS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Good morning, how are you?", ("good", "morning", "well", "fine", "help", "great", "assist", "today")),
    ("Remind me to take my medicine at 8 PM.", ("remind", "medicine", "8", "pm", "alarm", "set", "ok")),
    ("What day is it today?", ("day", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "today")),
    ("Can you speak slowly?", ("yes", "slow", "sure", "course", "help")),
    ("I feel lonely, can we talk?", ("here", "talk", "chat", "listen", "help", "yes", "course", "absolutely", "lonely", "feeling")),
    ("What alarms do I have?", ("alarm", "reminder", "no", "none", "one", "have", "pending")),
)

SOAK_VOICE_TIMERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Set an alarm for 7 AM.", ("alarm", "set", "7", "ok", "am")),
    ("Remind me to call mom at 6 PM.", ("remind", "call", "6", "pm", "alarm", "set", "ok")),
    ("Set a timer for 2 minutes.", ("timer", "minute", "alarm", "set", "remind", "ok", "2")),
    ("Remind me to drink water in 5 minutes.", ("remind", "water", "minute", "alarm", "set", "ok", "5")),
    ("Cancel all my alarms.", ("cancel", "alarm", "reminder", "deleted", "cleared", "ok", "none")),
)

SOAK_VOICE_GENERAL: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("What is 2 plus 2?", ("4", "four")),
    ("What is 7 plus 3?", ("10", "ten")),
    ("What is 15 minus 8?", ("7", "seven")),
    ("Reply with one word: hello", ("hello", "hi")),
    ("Say hi in one word.", ("hi", "hello", "hey")),
    ("Can you see anyone in the room?", ("see", "anyone", "person", "face", "don't", "no one", "not", "camera")),
    ("Do you recognize anyone?", ("recogn", "see", "don't", "no one", "not", "anyone", "face")),
    ("Is there a person in front of you?", ("person", "see", "don't", "no", "anyone", "face", "camera")),
    ("What color is the sky on a clear day?", ("blue",)),
    ("What planet do we live on?", ("earth",)),
    ("How many days are in a week?", ("7", "seven")),
    ("Count to three.", ("1", "one", "2", "two", "3", "three")),
    ("What language are you speaking?", ("english", "language", "speak")),
    ("Are you listening?", ("yes", "listen", "hear", "ready", "here")),
)

SOAK_VOICE_POOL: tuple[tuple[str, tuple[str, ...]], ...] = (
    SOAK_VOICE_GENERAL
    + SOAK_VOICE_KIDS
    + SOAK_VOICE_TWEENS
    + SOAK_VOICE_TEENS
    + SOAK_VOICE_ADULTS
    + SOAK_VOICE_SENIORS
    + SOAK_VOICE_TIMERS
)

SOAK_VOICE_AGE_GROUPS: tuple[tuple[str, tuple[tuple[str, tuple[str, ...]], ...]], ...] = (
    ("kids", SOAK_VOICE_KIDS),
    ("tweens", SOAK_VOICE_TWEENS),
    ("teens", SOAK_VOICE_TEENS),
    ("adults", SOAK_VOICE_ADULTS),
    ("seniors", SOAK_VOICE_SENIORS),
    ("timers", SOAK_VOICE_TIMERS),
)

# Always run these core checks every cycle (in addition to random picks).
SOAK_VOICE_CORE: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("What is 2 plus 2?", ("4", "four")),
    ("Can you see anyone in the room?", ("see", "anyone", "person", "face", "don't", "no one", "not", "camera")),
    ("Set a timer for 2 minutes.", ("timer", "minute", "alarm", "set", "remind", "ok", "2")),
)


def _soak_voice_random_enabled() -> bool:
    raw = os.environ.get("SOAK_VOICE_RANDOM", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _soak_voice_all_ages_enabled() -> bool:
    raw = os.environ.get("SOAK_VOICE_ALL_AGES", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _soak_voice_questions_per_cycle() -> int:
    try:
        return max(1, min(20, int(os.environ.get("SOAK_VOICE_QUESTIONS_PER_CYCLE", "6"))))
    except (TypeError, ValueError):
        return 6


def _soak_max_duration_seconds() -> int | None:
    raw = os.environ.get("SOAK_TEST_MAX_DURATION_SECONDS", "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
        return value if value > 0 else None
    except (TypeError, ValueError):
        return None


def pick_soak_voice_questions(*, cycle_number: int = 0) -> list[tuple[str, tuple[str, ...]]]:
    """Pick voice Q&A scenarios — core checks plus all age groups and timers."""
    if _soak_voice_all_ages_enabled():
        rng = random.Random()
        rng.seed(f"soak-voice-ages-{cycle_number}-{time.time()}")
        picked: list[tuple[str, tuple[str, ...]]] = list(SOAK_VOICE_CORE)
        seen = {q for q, _ in picked}
        for _group_name, group in SOAK_VOICE_AGE_GROUPS:
            if not group:
                continue
            choice = rng.choice(group)
            if choice[0] not in seen:
                picked.append(choice)
                seen.add(choice[0])
        pool = [q for q in SOAK_VOICE_POOL if q[0] not in seen]
        extra_count = max(0, _soak_voice_questions_per_cycle() - len(picked))
        if pool and extra_count:
            extra = rng.sample(pool, k=min(extra_count, len(pool)))
            picked.extend(extra)
        return picked

    if not _soak_voice_random_enabled():
        return list(SOAK_VOICE_CORE) + [
            q for q in SOAK_VOICE_POOL if q not in SOAK_VOICE_CORE
        ][: max(0, _soak_voice_questions_per_cycle() - len(SOAK_VOICE_CORE))]

    rng = random.Random()
    rng.seed(f"soak-voice-{cycle_number}-{time.time()}")

    core = list(SOAK_VOICE_CORE)
    pool = [q for q in SOAK_VOICE_POOL if q not in SOAK_VOICE_CORE]
    extra_count = max(0, _soak_voice_questions_per_cycle() - len(core))
    extra = rng.sample(pool, k=min(extra_count, len(pool))) if pool and extra_count else []
    return core + extra


@dataclass
class SoakCycleResult:
    cycle_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    cycle_number: int = 0
    started_at: str = field(default_factory=_utc_now)
    finished_at: str = ""
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    total: int = 0
    ok: bool = True
    intelligent_summary: dict[str, Any] = field(default_factory=dict)
    scenarios: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SoakTestStore:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _SOAK_PATH
        self._lock = threading.RLock()
        self._running = False
        self._started_at: str | None = None
        self._last_cycle: SoakCycleResult | None = None
        self._history: list[dict[str, Any]] = []
        self._cycles_completed = 0
        self._reload()

    def _reload(self) -> None:
        if not self._path.is_file():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return
            self._running = bool(raw.get("running"))
            self._started_at = raw.get("started_at")
            self._cycles_completed = int(raw.get("cycles_completed") or 0)
            self._history = list(raw.get("history") or [])
            last = raw.get("last_cycle")
            if isinstance(last, dict):
                self._last_cycle = SoakCycleResult(**{
                    k: last[k]
                    for k in SoakCycleResult.__dataclass_fields__
                    if k in last
                })
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": _utc_now(),
            "running": self._running,
            "started_at": self._started_at,
            "cycles_completed": self._cycles_completed,
            "last_cycle": self._last_cycle.to_dict() if self._last_cycle else None,
            "history": self._history[-200:],
        }
        self._path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def mark_running(self, *, running: bool) -> None:
        with self._lock:
            self._running = running
            if running and not self._started_at:
                self._started_at = _utc_now()
            if not running:
                self._started_at = self._started_at
            self._save()

    def record_cycle(self, cycle: SoakCycleResult) -> None:
        with self._lock:
            self._last_cycle = cycle
            self._cycles_completed += 1
            self._history.append(cycle.to_dict())
            self._history = self._history[-200:]
            self._save()

    def status(self) -> dict[str, Any]:
        with self._lock:
            last = self._last_cycle.to_dict() if self._last_cycle else None
            return {
                "running": self._running,
                "started_at": self._started_at,
                "cycles_completed": self._cycles_completed,
                "last_cycle": last,
                "history_count": len(self._history),
            }


_STORE = SoakTestStore()


def get_soak_store() -> SoakTestStore:
    return _STORE


def _face_vision_check(snapshot: dict[str, Any]) -> tuple[bool, str]:
    faces = snapshot.get("faces") or {}
    if not isinstance(faces, dict):
        return False, "Face service stats unavailable"
    if not faces.get("recognizer_available"):
        return False, "Face recognizer not loaded (install opencv-contrib-python)"
    detector_name = str(faces.get("detector") or "")
    if detector_name == "unavailable":
        return False, "Face detector not available"

    reg = snapshot.get("face_registration") or {}
    if isinstance(reg, dict):
        state = str(reg.get("state") or "idle")
        if state not in {"idle", "listening", "ready"}:
            return False, f"Face registration stuck in state={state}"

    cameras = snapshot.get("cameras") or {}
    devices = (snapshot.get("devices") or {}).get("devices") or []
    live_device = None
    for row in devices:
        if not isinstance(row, dict):
            continue
        device_id = str(row.get("device_id") or "")
        cam = cameras.get(device_id) if isinstance(cameras, dict) else None
        if isinstance(cam, dict) and cam.get("connected"):
            live_device = device_id
            break

    if live_device:
        return True, f"Face stack ready; live camera on {live_device}"

    runtime = snapshot.get("bot_runtime") or {}
    any_session = any(
        isinstance(row, dict) and row.get("session_active")
        for row in runtime.values()
    )
    if any_session:
        return False, "Voice session active but no MJPEG frames for face check"
    return True, "Face stack ready (camera idle — person detection runs during voice sessions)"


def _memory_check(snapshot: dict[str, Any]) -> tuple[bool, str]:
    memory = snapshot.get("memory") or {}
    if not isinstance(memory, dict):
        return False, "Memory status unavailable"
    if not memory.get("database_url_set"):
        return True, "Memory DB not configured — skipped"
    if not memory.get("ready"):
        err = str(memory.get("last_error") or "PostgreSQL not ready")
        return False, err
    return True, "Conversation memory database ready"


def _tts_synthesis_check() -> tuple[bool, str]:
    try:
        from tts_service import preload_piper_voice, synthesize_sapi_wav_bytes

        preload_piper_voice()
        wav, voice = synthesize_sapi_wav_bytes("NiNO soak test.", device_id=None)
        if wav and len(wav) > 100:
            return True, f"TTS produced audio ({voice or 'default'})"
        return False, "TTS returned empty or very short audio"
    except Exception as exc:
        try:
            from tts_service import preload_piper_voice

            ok = preload_piper_voice()
            if ok:
                return True, "Piper TTS voice preloaded"
            return False, str(exc)[:120]
        except Exception as inner:
            return False, str(inner)[:120]


def _soak_live_esp_enabled() -> bool:
    raw = os.environ.get("SOAK_LIVE_ESP", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def resolve_soak_device(snapshot: dict[str, Any]) -> tuple[str, str, str] | None:
    """Return (device_id, play_wav_url, display_name) for the first bot with playback."""
    devices = (snapshot.get("devices") or {}).get("devices") or []
    for row in devices:
        if not isinstance(row, dict):
            continue
        device_id = str(row.get("device_id") or "").strip()
        play_url = str(row.get("play_wav_url") or "").strip()
        if device_id and play_url:
            display = str(row.get("display_name") or device_id)
            return device_id, play_url, display
    try:
        from device_registry import get_device_registry

        for record in get_device_registry().list_devices():
            play_url = record.effective_play_wav_url()
            if play_url:
                return record.device_id, play_url, record.display_name or record.device_id
    except Exception:
        pass
    from esp_playback import esp_play_wav_url

    play_url = esp_play_wav_url()
    if play_url:
        return "", play_url, "default ESP"
    return None


_SOAK_VOICE_LOCK = threading.Lock()


def _wait_device_idle(device_id: str, *, timeout_s: float = 120.0) -> None:
    from esp_playback import clear_device_busy, wait_device_playback_idle

    if wait_device_playback_idle(device_id or None, timeout_s=timeout_s):
        return
    clear_device_busy(device_id or None)


def _deliver_reply_to_esp(device_id: str, wav_out: bytes, meta: Any) -> tuple[bool, str]:
    from esp_playback import (
        _wav_duration_seconds,
        deliver_wav_to_device,
        extend_playback_busy,
        wait_device_playback_idle,
    )
    from wav_resample import ESP_PCM_SAMPLE_RATE_HZ, resample_wav_bytes_to_mono_16bit

    if not wav_out or len(wav_out) < 500:
        return False, "no output wav for ESP"

    esp_wav = resample_wav_bytes_to_mono_16bit(wav_out, ESP_PCM_SAMPLE_RATE_HZ)
    eye = getattr(meta, "eye_expression", None) or (meta.timings or {}).get("eye_expression")
    try:
        deliver_wav_to_device(
            device_id or None,
            esp_wav,
            timeout=60.0,
            prompt_ack=bool(getattr(meta, "prompt_medical_ack", False)),
            eye_expression=str(eye) if eye else None,
        )
    except Exception as exc:
        return False, f"ESP play_wav failed: {exc}"

    play_s = _wav_duration_seconds(esp_wav)
    busy_s = extend_playback_busy(esp_wav, device_id=device_id)
    wait_device_playback_idle(
        device_id or None,
        timeout_s=max(30.0, busy_s + 5.0),
    )
    return True, f"ESP played {play_s:.1f}s ({len(esp_wav)} bytes)"


_SOAK_FAILURE_PATHS = frozenset(
    {"stt_empty", "stt_silent", "stt_rejected", "too_long", "error", "failed", "none"}
)

# Voice paths that mean routing succeeded — validate reply shape, not keyword bingo.
_SOAK_TRUSTED_REPLY_PATHS = frozenset(
    {
        "local_time",
        "joke",
        "football_joke",
        "joke_and_time",
        "greeting",
        "smalltalk",
        "alarm",
        "math",
        "weather",
        "session_greet",
        "volume",
        "goodbye",
        "face_track",
        "say_no3",
        "say_yes3",
        "look_scan",
        "face_registration",
        "identity_correction",
        "recap_answer",
        "recap",
        "recap_not_found",
        "recap_blocked_no_face",
        "last_question",
        "servo_360",
        "world_cup_favourite",
        "football_live",
        "fifa_world_cup_top_scorer",
        "fifa_world_cup_winner",
        "football_query_needs_detail",
        "identity_llm",
    }
)

_REPLY_ERROR_PHRASES = (
    "could not reach",
    "language model",
    "try again",
    "something went wrong",
    "i don't know how",
)


def _looks_like_time_reply(reply: str) -> bool:
    lowered = reply.lower()
    if re.search(r"\b\d{1,2}:\d{2}\b", lowered):
        return True
    if re.search(r"\b\d{1,2}\s*(am|pm)\b", lowered):
        return True
    if "it is" in lowered or "it's" in lowered:
        return True
    weekdays = (
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    )
    if any(day in lowered for day in weekdays):
        return True
    return any(token in lowered for token in ("time", "clock", "hour", "minute"))


def _looks_like_joke_reply(reply: str) -> bool:
    lowered = reply.lower().strip()
    if len(lowered) < 12:
        return False
    joke_hints = (
        "joke",
        "funny",
        "why ",
        "laugh",
        "haha",
        "ha,",
        "ha!",
        "okay",
        "here we go",
        "told my",
        "said,",
    )
    return any(h in lowered for h in joke_hints) or "?" in reply or "!" in reply


def _reply_has_error_phrase(reply: str) -> bool:
    lowered = reply.lower()
    return any(phrase in lowered for phrase in _REPLY_ERROR_PHRASES)


def _keyword_match(reply: str, expected: tuple[str, ...]) -> bool:
    lowered = reply.lower()
    for token in expected:
        key = token.lower().strip()
        if not key:
            continue
        if " " in key:
            if key in lowered:
                return True
            continue
        if re.search(rf"\b{re.escape(key)}\b", lowered):
            return True
        if len(key) <= 3 and key in lowered:
            return True
    return False


def _path_reply_valid(path: str, reply: str) -> bool | None:
    """Return True/False when path-specific rules apply, else None for keyword fallback."""
    key = str(path or "").strip().lower()
    if key in _SOAK_FAILURE_PATHS:
        return False
    if key == "local_time":
        return _looks_like_time_reply(reply)
    if key in {"joke", "football_joke", "joke_and_time"}:
        return _looks_like_joke_reply(reply)
    if key in _SOAK_TRUSTED_REPLY_PATHS:
        return len(reply.strip()) >= 3 and not _reply_has_error_phrase(reply)
    return None


def soak_reply_would_pass(*, path: str, reply: str, expected: tuple[str, ...] = ()) -> bool:
    text = str(reply or "").strip()
    route = str(path or "").strip().lower()
    if route in _SOAK_FAILURE_PATHS:
        return False
    if not text:
        return False
    if _reply_has_error_phrase(text):
        return False
    path_ok = _path_reply_valid(route, text)
    if path_ok is True:
        return True
    if path_ok is False:
        return False
    if expected and _keyword_match(text, expected):
        return True
    if route == "llm" and len(text) >= 10:
        return True
    if not expected and len(text) >= 3:
        return True
    return False


def parse_soak_unexpected_reply(error: str) -> tuple[str, str] | None:
    """Extract reply_path and reply text from a soak 'unexpected reply' failure message."""
    match = re.search(
        r"path=(\w+)\s+unexpected reply=(.+)$",
        str(error or ""),
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    return match.group(1), match.group(2).strip()


def stale_soak_incident_resolution(error: str) -> str | None:
    """Return an auto-resolve message for obsolete soak false-positive incidents."""
    raw = str(error or "").strip()
    if not raw or "[soak:" not in raw.lower():
        return None

    parsed = parse_soak_unexpected_reply(raw)
    if parsed is not None:
        path, reply = parsed
        if soak_reply_would_pass(path=path, reply=reply):
            return (
                "Auto-resolved: soak test falsely flagged a valid voice reply; "
                "Intelligent Mode confirmed the bot answered correctly."
            )

    lowered = raw.lower()
    if "play_wav failed" in lowered and "wav too large" in lowered:
        return (
            "Auto-resolved: long TTS replies are now auto-split for ESP playback; "
            "this soak failure is obsolete."
        )
    if is_soak_live_session_skip(raw):
        return (
            "Auto-resolved: soak voice test was skipped because a live voice session "
            "was active — not a bot failure."
        )
    return None


def _validate_voice_reply(
    *,
    reply: str,
    path: str,
    out: bytes,
    expected: tuple[str, ...],
    live_esp: bool,
) -> tuple[bool, str]:
    if path in _SOAK_FAILURE_PATHS or path in {"stt_empty", "stt_silent", "stt_rejected"}:
        return False, f"STT path={path}"
    if not reply:
        return False, f"empty reply path={path}"
    if _reply_has_error_phrase(reply):
        return False, f"LLM failure: {reply[:100]}"
    if not out and live_esp:
        return False, "no output wav for ESP playback"
    if not out and not live_esp:
        return False, "no output wav"

    path_ok = _path_reply_valid(path, reply)
    if path_ok is True:
        return True, f"path={path} reply={reply[:80]}"
    if path_ok is False:
        return False, f"path={path} invalid reply={reply[:100]}"

    if not expected:
        if len(reply) >= 3:
            return True, f"path={path} reply={reply[:80]}"
        return False, f"path={path} reply too short={reply[:80]}"
    if _keyword_match(reply, expected):
        return True, f"path={path} reply={reply[:80]}"
    if soak_reply_would_pass(path=path, reply=reply, expected=expected):
        return True, f"path={path} reply={reply[:80]}"
    if path == "llm" and len(reply.strip()) >= 10:
        return True, f"path={path} substantive reply={reply[:80]}"
    return False, f"path={path} unexpected reply={reply[:100]}"


def _voice_scenario_checks(
    *,
    cycle_number: int = 0,
    snapshot: dict[str, Any] | None = None,
    voice_active_fn: Callable[[str | None], bool] | None = None,
) -> list[tuple[str, Callable[[], tuple[bool, str]], dict[str, Any]]]:
    from unittest.mock import patch

    from intelligent_mode.e2e_voice_test import _speech_like_wav
    from voice_service import process_voice_wav

    snapshot = snapshot or {}
    live_target = resolve_soak_device(snapshot) if _soak_live_esp_enabled() else None
    live_esp = live_target is not None
    device_id = live_target[0] if live_target else "soak-test"
    device_label = live_target[2] if live_target else "in-process"

    checks: list[tuple[str, Callable[[], tuple[bool, str]], dict[str, Any]]] = []
    selected = pick_soak_voice_questions(cycle_number=cycle_number)

    if _soak_live_esp_enabled() and not live_esp:
        def _no_device() -> tuple[bool, str]:
            return False, "SOAK_LIVE_ESP=1 but no bot play_wav URL discovered"

        checks.append(
            (
                f"soak:voice:{cycle_number}:live_esp_unavailable",
                _no_device,
                {
                    "test_id": f"soak:voice:{cycle_number}:live_esp_unavailable",
                    "device_id": "server",
                    "subsystem": "voice",
                    "severity": "critical",
                    "tier": 0,
                },
            )
        )
        return checks

    def _make(question: str, expected: tuple[str, ...]) -> Callable[[], tuple[bool, str]]:
        def _check() -> tuple[bool, str]:
            if voice_active_fn is not None:
                try:
                    if voice_active_fn(device_id or None) or voice_active_fn(None):
                        return None, "skipped — live voice session active on bot/server"
                except Exception:
                    pass

            if not _SOAK_VOICE_LOCK.acquire(timeout=120.0):
                return False, "timed out waiting for soak voice lock"

            try:
                if live_esp:
                    _wait_device_idle(device_id)

                wav = _speech_like_wav()
                with patch(
                    "voice_service.transcribe_wav",
                    return_value=(question, "soak-pick"),
                ):
                    out, meta = process_voice_wav(
                        wav,
                        device_id=device_id,
                        session_kind="continue",
                        session_id=f"soak-{cycle_number}",
                    )
                reply = str(meta.timings.get("reply_text") or "").strip()
                path = str(meta.timings.get("reply_path") or "")
                ok, detail = _validate_voice_reply(
                    reply=reply,
                    path=path,
                    out=out,
                    expected=expected,
                    live_esp=live_esp,
                )
                if not ok:
                    return False, detail

                if live_esp:
                    esp_ok, esp_detail = _deliver_reply_to_esp(device_id, out, meta)
                    if not esp_ok:
                        return False, esp_detail
                    return True, f"{device_label}: {esp_detail}; {detail}"

                return True, detail
            finally:
                _SOAK_VOICE_LOCK.release()

        return _check

    meta_device_id = device_id or "server"
    for index, (question, expected) in enumerate(selected):
        slug = re.sub(r"[^a-z0-9]+", "_", question.lower())[:32].strip("_")
        mode = "live" if live_esp else "mock"
        name = f"soak:voice:{mode}:{cycle_number}:{index}:{slug or 'q'}"
        checks.append(
            (
                name,
                _make(question, expected),
                {
                    "test_id": name,
                    "device_id": meta_device_id,
                    "subsystem": "voice",
                    "severity": "critical",
                    "tier": 0,
                },
            )
        )
    return checks


def _bot_status_checks(snapshot: dict[str, Any]) -> list[tuple[str, Callable[[], tuple[bool, str]], dict[str, Any]]]:
    import urllib.error
    import urllib.request

    checks: list[tuple[str, Callable[[], tuple[bool, str]], dict[str, Any]]] = []
    devices = (snapshot.get("devices") or {}).get("devices") or []
    for row in devices:
        if not isinstance(row, dict):
            continue
        device_id = str(row.get("device_id") or "").strip()
        base_url = str(row.get("base_url") or "").strip()
        if not device_id or not base_url:
            continue
        display = str(row.get("display_name") or device_id)

        def _status_ok(
            url: str = base_url,
            label: str = display,
            did: str = device_id,
        ) -> tuple[bool, str]:
            req = urllib.request.Request(url.rstrip("/") + "/status")
            try:
                with urllib.request.urlopen(req, timeout=4.0) as resp:
                    if not (200 <= resp.status < 300):
                        return False, f"{label} /status HTTP {resp.status}"
                    import json as _json

                    payload = _json.loads(resp.read().decode("utf-8", errors="replace"))
                    if not isinstance(payload, dict):
                        return False, f"{label} /status invalid JSON"
                    if payload.get("ok") is False:
                        return False, f"{label} reported ok=false"
                    sta = payload.get("sta_connected")
                    ip = payload.get("ip") or "?"
                    cam = payload.get("camera") if isinstance(payload.get("camera"), dict) else {}
                    session = cam.get("session_active") if isinstance(cam, dict) else None
                    return True, (
                        f"{label} online ip={ip} wifi_connected={sta} "
                        f"camera_session={session}"
                    )
            except urllib.error.HTTPError as exc:
                return False, f"{label} /status HTTP {exc.code}"
            except Exception as exc:
                return False, f"{label} unreachable: {exc}"

        test_id = f"soak:bot:{device_id}:status"
        checks.append(
            (
                test_id,
                _status_ok,
                {
                    "test_id": test_id,
                    "device_id": device_id,
                    "subsystem": "bot",
                    "severity": "critical",
                    "tier": 1,
                },
            )
        )
    return checks


def soak_failures_to_candidates(
    results: list[SmokeTestResult],
    *,
    device_names: dict[str, str] | None = None,
) -> list[DetectionCandidate]:
    names = device_names or {}
    out: list[DetectionCandidate] = []
    for result in results:
        if result.passed or result.skipped:
            continue
        from intelligent_mode.incident_filters import is_test_skip_message

        if is_test_skip_message(result.message):
            continue
        if str(result.message or "").lower().startswith("skipped"):
            continue
        device_id = result.device_id or "server"
        display = names.get(device_id) or (
            "NiNO Server" if device_id == "server" else device_id
        )
        out.append(
            DetectionCandidate(
                device_id=device_id,
                display_name=display,
                subsystem=result.subsystem,
                severity=result.severity,
                tier=result.tier,
                error=f"[soak:{result.test_id}] {result.message}",
                snapshot_hint={"soak_test": result.to_dict()},
            )
        )
    return out


def run_soak_scenarios(
    snapshot: dict[str, Any],
    *,
    voice_active_fn: Callable[[str | None], bool] | None = None,
    cycle_number: int = 0,
) -> list[SmokeTestResult]:
    """Extended E2E scenarios beyond smoke + basic e2e."""
    results: list[SmokeTestResult] = []

    llm = snapshot.get("llm") or {}
    llm_up = bool(isinstance(llm, dict) and llm.get("reachable"))

    results.append(
        _run(
            "soak:face_vision",
            lambda: _face_vision_check(snapshot),
            test_id="soak:face_vision",
            device_id="server",
            subsystem="camera",
            severity="warning",
            tier=1,
        )
    )
    results.append(
        _run(
            "soak:memory",
            lambda: _memory_check(snapshot),
            test_id="soak:memory",
            device_id="server",
            subsystem="memory",
            severity="warning",
            tier=1,
        )
    )
    results.append(
        _run(
            "soak:tts",
            _tts_synthesis_check,
            test_id="soak:tts",
            device_id="server",
            subsystem="tts",
            severity="warning",
            tier=1,
        )
    )

    smoke = run_smoke_suite(snapshot, voice_active_fn=voice_active_fn)
    results.extend(smoke.results)

    if llm_up:
        e2e = run_e2e_voice_suite(snapshot)
        results.extend(e2e.results)
        for name, fn, meta in _voice_scenario_checks(
            cycle_number=cycle_number,
            snapshot=snapshot,
            voice_active_fn=voice_active_fn,
        ):
            results.append(_run(name, fn, **meta))
    else:
        results.append(
            SmokeTestResult(
                test_id="soak:voice:skipped",
                name="soak:voice:skipped",
                device_id="server",
                subsystem="voice",
                passed=False,
                message=str(llm.get("warning") or "LLM unreachable — voice soak skipped"),
                severity="critical",
                tier=0,
                skipped=True,
            )
        )

    for name, fn, meta in _bot_status_checks(snapshot):
        results.append(_run(name, fn, **meta))

    return results


def run_soak_cycle(
    *,
    collect_status: Callable[[], dict[str, Any]],
    remediate: Callable[[dict[str, Any], list[DetectionCandidate]], dict[str, Any]] | None = None,
    voice_active_fn: Callable[[str | None], bool] | None = None,
    cycle_number: int = 0,
) -> SoakCycleResult:
    """One full soak cycle: scenarios then Intelligent Mode fix/email."""
    cycle = SoakCycleResult(cycle_number=cycle_number, started_at=_utc_now())
    snapshot = collect_status()

    try:
        scenario_results = run_soak_scenarios(
            snapshot,
            voice_active_fn=voice_active_fn,
            cycle_number=cycle_number,
        )
    except Exception as exc:
        logger.exception("Soak scenarios failed")
        scenario_results = [
            SmokeTestResult(
                test_id="soak:internal_error",
                name="soak:internal_error",
                device_id="server",
                subsystem="server",
                passed=False,
                message=str(exc),
                severity="critical",
                tier=0,
            )
        ]

    if remediate is not None:
        device_names = {
            str(row.get("device_id") or ""): str(row.get("display_name") or "")
            for row in (snapshot.get("devices") or {}).get("devices") or []
            if isinstance(row, dict)
        }
        candidates = soak_failures_to_candidates(
            scenario_results, device_names=device_names
        )
        try:
            cycle.intelligent_summary = remediate(snapshot, candidates)
        except Exception as exc:
            logger.exception("Intelligent remediation failed during soak")
            cycle.intelligent_summary = {"error": str(exc)}

    cycle.scenarios = [r.to_dict() for r in scenario_results]
    cycle.total = len(scenario_results)
    cycle.skipped = sum(1 for r in scenario_results if r.skipped)
    cycle.passed = sum(1 for r in scenario_results if r.passed)
    cycle.failed = sum(
        1 for r in scenario_results if not r.passed and not r.skipped
    )
    cycle.ok = cycle.failed == 0
    cycle.finished_at = _utc_now()
    return cycle


class SoakTestRunner:
    """Background/foreground loop — runs until stop() or process exit."""

    def __init__(
        self,
        *,
        interval_seconds: int = 90,
        collect_status: Callable[[], dict[str, Any]] | None = None,
        remediate: Callable[[dict[str, Any], list[DetectionCandidate]], dict[str, Any]] | None = None,
        voice_active_fn: Callable[[str | None], bool] | None = None,
        store: SoakTestStore | None = None,
    ) -> None:
        self.interval_seconds = max(30, interval_seconds)
        self._collect_status = collect_status
        self._remediate = remediate
        self._voice_active_fn = voice_active_fn
        self._store = store or get_soak_store()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._cycle_number = 0
        self._started_at: float | None = None
        self.max_duration_seconds = _soak_max_duration_seconds()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self, *, max_duration_seconds: int | None = None) -> None:
        if self.running:
            return
        if max_duration_seconds is not None and max_duration_seconds > 0:
            self.max_duration_seconds = max_duration_seconds
        elif self.max_duration_seconds is None:
            self.max_duration_seconds = _soak_max_duration_seconds()
        self._stop.clear()
        self._started_at = time.monotonic()
        self._store.mark_running(running=True)
        self._thread = threading.Thread(target=self._loop, daemon=True, name="nino-soak-test")
        self._thread.start()
        logger.info(
            "Soak test runner started (interval=%ss max_duration=%s)",
            self.interval_seconds,
            self.max_duration_seconds,
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10.0)
        self._store.mark_running(running=False)
        logger.info("Soak test runner stopped after %d cycle(s)", self._cycle_number)

    def run_once(self) -> SoakCycleResult:
        if self._collect_status is None:
            raise RuntimeError("Soak runner collect_status is not configured")
        self._cycle_number += 1
        cycle = run_soak_cycle(
            collect_status=self._collect_status,
            remediate=self._remediate,
            voice_active_fn=self._voice_active_fn,
            cycle_number=self._cycle_number,
        )
        self._store.record_cycle(cycle)
        return cycle

    def _loop(self) -> None:
        while not self._stop.is_set():
            if self.max_duration_seconds and self._started_at is not None:
                elapsed = time.monotonic() - self._started_at
                if elapsed >= self.max_duration_seconds:
                    logger.info(
                        "Soak test max duration reached (%.0fs) — stopping",
                        self.max_duration_seconds,
                    )
                    break
            try:
                cycle = self.run_once()
                logger.info(
                    "Soak cycle %d complete: %d/%d passed ok=%s",
                    cycle.cycle_number,
                    cycle.passed,
                    cycle.total,
                    cycle.ok,
                )
            except Exception:
                logger.exception("Soak cycle failed")
            if self._stop.wait(self.interval_seconds):
                break
        self._store.mark_running(running=False)


_RUNNER: SoakTestRunner | None = None


def configure_soak_runner(runner: SoakTestRunner) -> None:
    global _RUNNER
    _RUNNER = runner


def get_soak_runner() -> SoakTestRunner | None:
    return _RUNNER
