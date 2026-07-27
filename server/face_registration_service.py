"""Automatic voice-triggered face registration (unknown face → prompt → name → capture)."""

from __future__ import annotations

import logging
import os
import random
import threading
import time
import wave
import io
from dataclasses import dataclass
from typing import Any, Callable, Literal

import numpy as np

from esp_playback import deliver_wav_to_device, device_base_url
from face_registration_voice import (
    extract_registration_name,
    is_face_reg_prompt_echo,
    is_incomplete_name_phrase,
    is_registration_cancel,
)
from tts_service import synthesize_sapi_wav_bytes
from wav_resample import resample_wav_bytes_to_mono_16bit

logger = logging.getLogger(__name__)

FaceRegState = Literal["idle", "awaiting_name", "capturing"]

REGISTRATION_PROMPTS: tuple[str, ...] = (
    "I don't think we have met before. After the beep, please say your name.",
    "Ooh, a mystery guest! After the beep, please say your name.",
    "Fresh face alert! After the beep, please say your name.",
    "Hold up… who are you? After the beep, please say your name.",
    "I sense a new friend. After the beep, please say your name.",
    "Welcome to the show! After the beep, please say your name so I can remember you.",
)

# Back-compat alias (first variant); prefer pick_registration_prompt().
REGISTRATION_PROMPT = REGISTRATION_PROMPTS[0]


def pick_registration_prompt() -> str:
    """Random entertaining prompt for an unknown face."""
    return random.choice(REGISTRATION_PROMPTS)

NAME_RETRY_PROMPT = (
    "I didn't catch your name. After the beep, please say your name again."
)

INCOMPLETE_NAME_PROMPT = (
    "I only heard part of that. After the beep, please say your name again."
)

NO_SPEECH_RETRY_PROMPT = (
    "I haven't heard anything. After the beep, please say your name."
)

REGISTRATION_CANCEL_REPLY = (
    "Sorry — I won't register your face right now."
)

# Sentinel: reopen mic with beep only (no spoken prompt — avoids TTS echo loops).
_SILENT_RELISTEN = ""

VOICE_REG_TTS_RATE_HZ = 16000

_service: FaceRegistrationService | None = None


@dataclass
class FaceRegVoiceResult:
    handled: bool = False
    reply: str = ""
    registered_name: str | None = None
    relisten_after_reply: bool = False
    already_registered_as: str | None = None


def already_registered_reply(existing_name: str) -> str:
    return (
        f"You're already registered as {existing_name}. "
        "I won't register your face under a different name."
    )


def same_person_refresh_reply(name: str) -> str:
    return f"All set, {name}. I've added more face samples for you."


@dataclass
class CaptureResult:
    saved_samples: int
    training: dict[str, Any]
    errors: list[str]


def capture_face_samples(
    faces: Any,
    read_frame: Callable[[], np.ndarray | None],
    name: str,
    *,
    samples: int = 15,
    interval_ms: int = 150,
) -> CaptureResult:
    """Capture N face crops and rebuild embeddings (shared by web + voice paths)."""
    saved = 0
    errors: list[str] = []

    probe = read_frame()
    if probe is not None and hasattr(faces, "validate_registration_name"):
        allowed, existing = faces.validate_registration_name(probe, name)
        if not allowed and existing:
            return CaptureResult(
                saved_samples=0,
                training={"people": 0, "samples": 0, "rebuilt": False},
                errors=[f"already_registered_as:{existing}"],
            )

    for i in range(samples):
        frame = probe if i == 0 and probe is not None else read_frame()
        if frame is None:
            errors.append("No camera frame available")
            time.sleep(interval_ms / 1000)
            continue
        try:
            faces.register_sample(name, frame)
            saved += 1
        except ValueError as exc:
            errors.append(str(exc))
        except Exception as exc:
            logger.exception("Register sample failed")
            errors.append(f"Register error: {exc}")
        time.sleep(interval_ms / 1000)

    training: dict[str, Any] = {"people": 0, "samples": saved, "rebuilt": False}
    if saved > 0:
        try:
            faces.persist_embeddings()
        except Exception:
            logger.exception("Failed to persist face embeddings after register")
        try:
            training = {**faces.train(), "rebuilt": True}
        except Exception as exc:
            logger.exception("Post-register train failed; samples still saved")
            errors.append(f"Train error: {exc}")

    return CaptureResult(saved_samples=saved, training=training, errors=errors)


class FaceRegistrationService:
    def __init__(
        self,
        faces: Any,
        read_frame: Callable[[], np.ndarray | None],
    ) -> None:
        self._faces = faces
        self._read_frame = read_frame
        self._lock = threading.Lock()
        self._state: FaceRegState = "idle"
        self._unknown_since: float | None = None
        self._last_prompt_at: float = 0.0
        self._awaiting_since: float = 0.0
        self._listen_prompt_at: float = 0.0
        self._last_prompt_playback_seconds: float = 0.0
        self._voice_heard_since_listen: bool = False
        self._no_speech_retries: int = 0
        self._pending_relisten_prompt: str | None = NAME_RETRY_PROMPT
        self.apply_settings_from_environ()

    def set_frame_getter(self, read_frame: Callable[[], np.ndarray | None]) -> None:
        self._read_frame = read_frame

    def apply_settings_from_environ(self) -> None:
        self.enabled = os.environ.get("FACE_REG_ENABLED", "1").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self.unknown_seconds = float(os.environ.get("FACE_REG_UNKNOWN_SECONDS", "3"))
        self.prompt_cooldown_seconds = float(
            os.environ.get("FACE_REG_PROMPT_COOLDOWN_SECONDS", "600")
        )
        self.samples = max(1, min(80, int(os.environ.get("FACE_REG_SAMPLES", "15"))))
        self.interval_ms = max(
            50, min(2000, int(os.environ.get("FACE_REG_INTERVAL_MS", "150")))
        )
        self.await_name_seconds = float(
            os.environ.get("FACE_REG_AWAIT_NAME_SECONDS", "45")
        )
        self.no_speech_retry_seconds = float(
            os.environ.get("FACE_REG_NO_SPEECH_RETRY_SECONDS", "6")
        )
        self.listen_open_delay_seconds = float(
            os.environ.get("FACE_REG_LISTEN_OPEN_DELAY_SECONDS", "3")
        )
        self.max_no_speech_retries = max(
            0, int(os.environ.get("FACE_REG_MAX_NO_SPEECH_RETRIES", "2"))
        )
        self.unknown_confirm_frames = max(
            3, int(os.environ.get("FACE_REG_UNKNOWN_CONFIRM_FRAMES", "8"))
        )
        self._unknown_streak = 0

    @property
    def state(self) -> FaceRegState:
        with self._lock:
            return self._state

    def is_awaiting_name(self) -> bool:
        with self._lock:
            return self._state == "awaiting_name"

    def accepts_registration_voice(self, user_text: str) -> bool:
        """True when voice should be routed to face registration, not the LLM."""
        if not self.enabled:
            return False
        with self._lock:
            if self._state == "capturing":
                return False
            if self._state == "awaiting_name":
                return True
            if self._listen_prompt_at <= 0:
                return False
            if (time.time() - self._listen_prompt_at) > self.await_name_seconds:
                return False
        return extract_registration_name(user_text) is not None

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self.enabled,
                "state": self._state,
                "unknown_seconds": self.unknown_seconds,
                "prompt_cooldown_seconds": self.prompt_cooldown_seconds,
                "samples": self.samples,
                "interval_ms": self.interval_ms,
                "unknown_since": self._unknown_since,
                "last_prompt_at": self._last_prompt_at,
                "awaiting_since": self._awaiting_since,
                "listen_prompt_at": self._listen_prompt_at,
                "voice_heard_since_listen": self._voice_heard_since_listen,
                "no_speech_retries": self._no_speech_retries,
                "unknown_confirm_frames": self.unknown_confirm_frames,
                "unknown_streak": self._unknown_streak,
            }

    def on_frame(
        self,
        results: list[dict[str, Any]],
        *,
        voice_active: bool = False,
    ) -> None:
        """Called from MJPEG loop — may trigger proactive registration prompt."""
        if not self.enabled:
            return

        if self._primary_recognized(results):
            with self._lock:
                self._unknown_since = None
                self._unknown_streak = 0
                if (
                    self._state == "awaiting_name"
                    and not self._voice_heard_since_listen
                    and self._primary_confirmed_recognized(results)
                ):
                    logger.info(
                        "Face registration: cancelled — confirmed recognized face in frame"
                    )
                    self._reset_locked()
            return

        now = time.time()
        retry_prompt: str | None = None
        with self._lock:
            if self._state == "capturing":
                return
            if self._state == "awaiting_name":
                if voice_active:
                    self._awaiting_since = now
                    self._voice_heard_since_listen = True
                    return
                retry_prompt = self._plan_no_speech_retry_locked(now)
                if self._state != "awaiting_name":
                    return
                if retry_prompt is None:
                    if (
                        self._awaiting_since > 0
                        and (now - self._awaiting_since) > self.await_name_seconds
                    ):
                        logger.info("Face registration: awaiting_name timeout")
                        self._reset_locked()
                    return

        if retry_prompt:
            if not self._send_listen_prompt(retry_prompt):
                with self._lock:
                    self._reset_locked()
            return

        if voice_active:
            return

        if not self._primary_unknown_face(results):
            with self._lock:
                self._unknown_since = None
                self._unknown_streak = 0
            return

        with self._lock:
            self._unknown_streak += 1
            if self._unknown_streak < self.unknown_confirm_frames:
                return
            if self._unknown_since is None:
                self._unknown_since = now
                return
            stable_for = now - self._unknown_since
            if stable_for < self.unknown_seconds:
                return
            if (now - self._last_prompt_at) < self.prompt_cooldown_seconds:
                return

        self._try_prompt_registration()

    def on_voice_query_started(self) -> None:
        """ESP audio reached the server — cancel no-speech retry for this listen window."""
        with self._lock:
            if self._state == "awaiting_name":
                self._voice_heard_since_listen = True
                self._awaiting_since = time.time()

    def note_voice_received(self) -> None:
        """Mark that the ESP sent audio during the current listen window."""
        with self._lock:
            if self._state == "awaiting_name":
                self._voice_heard_since_listen = True

    def handle_voice(self, user_text: str) -> FaceRegVoiceResult:
        self.note_voice_received()

        with self._lock:
            if self._state == "idle":
                in_grace = (
                    self._listen_prompt_at > 0
                    and (time.time() - self._listen_prompt_at) <= self.await_name_seconds
                    and extract_registration_name(user_text) is not None
                )
                if in_grace:
                    logger.info(
                        "Face registration: resuming awaiting_name after listen-window reset"
                    )
                    self._state = "awaiting_name"
                    self._awaiting_since = time.time()
            if self._state != "awaiting_name":
                return FaceRegVoiceResult(handled=False)

        # Mic often captures our own TTS; reopen quietly instead of re-prompting.
        if is_face_reg_prompt_echo(user_text):
            logger.info(
                "Face registration: ignoring prompt echo | heard: %s",
                user_text[:80],
            )
            with self._lock:
                self._pending_relisten_prompt = _SILENT_RELISTEN
            return FaceRegVoiceResult(
                handled=True,
                reply="",
                relisten_after_reply=True,
            )

        if is_incomplete_name_phrase(user_text):
            logger.info(
                "Face registration: incomplete name phrase | heard: %s",
                user_text[:80],
            )
            with self._lock:
                self._pending_relisten_prompt = INCOMPLETE_NAME_PROMPT
            return FaceRegVoiceResult(
                handled=True,
                reply=INCOMPLETE_NAME_PROMPT,
                relisten_after_reply=True,
            )

        if is_registration_cancel(user_text):
            logger.info(
                "Face registration: cancelled by user | heard: %s",
                user_text[:80],
            )
            with self._lock:
                # Keep cooldown from the original prompt so we don't re-ask immediately.
                self._last_prompt_at = time.time()
                self._reset_locked()
            return FaceRegVoiceResult(
                handled=True,
                reply=REGISTRATION_CANCEL_REPLY,
                relisten_after_reply=False,
            )

        name = extract_registration_name(user_text)
        if not name:
            with self._lock:
                self._pending_relisten_prompt = NAME_RETRY_PROMPT
            return FaceRegVoiceResult(
                handled=True,
                reply=NAME_RETRY_PROMPT,
                relisten_after_reply=True,
            )

        frame = self._read_frame()
        existing_before: str | None = None
        if frame is not None and hasattr(self._faces, "identify_registered_face"):
            existing_before = self._faces.identify_registered_face(frame)
        if frame is not None and hasattr(self._faces, "validate_registration_name"):
            allowed, existing = self._faces.validate_registration_name(frame, name)
            if not allowed and existing:
                with self._lock:
                    self._reset_locked()
                logger.info(
                    "Face registration blocked: already %s, heard name %s",
                    existing,
                    name,
                )
                return FaceRegVoiceResult(
                    handled=True,
                    reply=already_registered_reply(existing),
                    already_registered_as=existing,
                )

        with self._lock:
            self._state = "capturing"

        logger.info("Face registration: capturing samples for %s", name)
        capture = capture_face_samples(
            self._faces,
            self._read_frame,
            name,
            samples=self.samples,
            interval_ms=self.interval_ms,
        )

        with self._lock:
            self._reset_locked()

        if capture.saved_samples == 0:
            detail = capture.errors[-1] if capture.errors else "No samples saved"
            if detail.startswith("already_registered_as:"):
                existing = detail.split(":", 1)[1]
                logger.info(
                    "Face registration blocked during capture: already %s, heard %s",
                    existing,
                    name,
                )
                return FaceRegVoiceResult(
                    handled=True,
                    reply=already_registered_reply(existing),
                    already_registered_as=existing,
                )
            logger.warning("Face registration failed for %s: %s", name, detail)
            return FaceRegVoiceResult(
                handled=True,
                reply=(
                    f"I heard {name}, but I couldn't capture your face. "
                    "Please look at the camera and try again."
                ),
            )

        logger.info(
            "Face registration complete | name=%s samples=%d training=%s",
            name,
            capture.saved_samples,
            capture.training,
        )
        refresh = bool(
            existing_before
            and hasattr(self._faces, "same_person")
            and self._faces.same_person(name, existing_before)
        )
        reply = (
            same_person_refresh_reply(name)
            if refresh
            else f"All set, {name}. I've registered your face."
        )
        return FaceRegVoiceResult(
            handled=True,
            reply=reply,
            registered_name=name,
        )

    def _try_prompt_registration(self) -> None:
        if device_base_url(None) is None:
            logger.debug("Face registration prompt skipped: no ESP play URL")
            return

        with self._lock:
            if self._state != "idle":
                return
            if (time.time() - self._last_prompt_at) < self.prompt_cooldown_seconds:
                return
            self._state = "awaiting_name"
            self._awaiting_since = time.time()
            self._last_prompt_at = self._awaiting_since
            self._unknown_since = None
            self._no_speech_retries = 0

        if not self._send_listen_prompt(pick_registration_prompt()):
            with self._lock:
                self._reset_locked()

    def relisten_after_missed_name(self) -> None:
        """Play beep (+ optional retry TTS) and open mic when the name was missed."""
        with self._lock:
            if self._state != "awaiting_name":
                return
            prompt = self._pending_relisten_prompt
            self._pending_relisten_prompt = NAME_RETRY_PROMPT
        if prompt == _SILENT_RELISTEN:
            self._send_silent_listen()
            return
        self._send_listen_prompt(prompt or NAME_RETRY_PROMPT)

    def _no_speech_check_after_locked(self) -> float:
        """Earliest time we may treat the listen window as silent. Holds lock."""
        return (
            self._listen_prompt_at
            + self._last_prompt_playback_seconds
            + self.listen_open_delay_seconds
            + self.no_speech_retry_seconds
        )

    def _plan_no_speech_retry_locked(self, now: float) -> str | None:
        """Return retry prompt text when the listen window passed with no voice. Holds lock."""
        if self._voice_heard_since_listen or self._listen_prompt_at <= 0:
            return None
        if now < self._no_speech_check_after_locked():
            return None
        if self._no_speech_retries >= self.max_no_speech_retries:
            logger.info(
                "Face registration: no speech after %d listen(s), giving up",
                self._no_speech_retries + 1,
            )
            self._reset_locked()
            return None
        self._no_speech_retries += 1
        logger.info(
            "Face registration: no speech detected, retry %d/%d",
            self._no_speech_retries,
            self.max_no_speech_retries,
        )
        return NO_SPEECH_RETRY_PROMPT

    @staticmethod
    def _wav_playback_seconds(wav: bytes, rate_hz: int = VOICE_REG_TTS_RATE_HZ) -> float:
        if len(wav) <= 44:
            return 0.0
        try:
            with wave.open(io.BytesIO(wav), "rb") as wf:
                frames = wf.getnframes()
                rate = wf.getframerate() or rate_hz
                return frames / float(rate)
        except wave.Error:
            pcm_bytes = max(0, len(wav) - 44)
            return pcm_bytes / (rate_hz * 2)

    def _send_listen_prompt(self, text: str) -> bool:
        if device_base_url(None) is None:
            logger.debug("Face registration listen prompt skipped: no ESP play URL")
            return False

        from device_registry import get_device_registry

        device_id = get_device_registry().ui_device_id()
        wav = self._synthesize_prompt_wav(text)
        try:
            deliver_wav_to_device(device_id, wav, prompt_ack=True, prompt_ack_chime=True)
        except Exception as exc:
            logger.warning("Face registration listen prompt failed: %s", exc)
            return False

        now = time.time()
        playback_s = self._wav_playback_seconds(wav)
        with self._lock:
            self._listen_prompt_at = now
            self._last_prompt_playback_seconds = playback_s
            self._voice_heard_since_listen = False
            self._awaiting_since = now
        logger.info(
            "Face registration listen prompt sent (%.1fs TTS + beep then listen): %s",
            playback_s,
            text[:80],
        )
        return True

    def _send_silent_listen(self) -> bool:
        """Reopen the mic with beep only — used when STT heard our own prompt."""
        if device_base_url(None) is None:
            logger.debug("Face registration silent listen skipped: no ESP play URL")
            return False

        from device_registry import get_device_registry
        from voice_service import minimal_voice_reply_wav

        device_id = get_device_registry().ui_device_id()
        wav = minimal_voice_reply_wav()
        try:
            deliver_wav_to_device(device_id, wav, prompt_ack=True, prompt_ack_chime=True)
        except Exception as exc:
            logger.warning("Face registration silent listen failed: %s", exc)
            return False

        now = time.time()
        playback_s = self._wav_playback_seconds(wav)
        with self._lock:
            self._listen_prompt_at = now
            self._last_prompt_playback_seconds = playback_s
            self._voice_heard_since_listen = False
            self._awaiting_since = now
        logger.info("Face registration silent listen opened (beep only)")
        return True

    @staticmethod
    def _synthesize_prompt_wav(text: str) -> bytes:
        wav, _ = synthesize_sapi_wav_bytes(text.strip())
        return resample_wav_bytes_to_mono_16bit(wav, VOICE_REG_TTS_RATE_HZ)

    def _primary_recognized(self, results: list[dict[str, Any]]) -> bool:
        return (
            self._primary_confirmed_recognized(results)
            or self._primary_pending_recognized(results)
        )

    @staticmethod
    def _primary_confirmed_recognized(results: list[dict[str, Any]]) -> bool:
        for result in results:
            if not result.get("primary", True):
                continue
            if result.get("recognized") or result.get("stabilized"):
                name = str(result.get("name", "")).strip().lower()
                if name and name not in {"unknown", "face"} and "[hold]" not in name:
                    return True
        return False

    def _primary_pending_recognized(self, results: list[dict[str, Any]]) -> bool:
        soft_threshold = float(
            getattr(self._faces, "match_soft_threshold", 0.42)
        )
        for result in results:
            if not result.get("primary", True):
                continue
            if result.get("pending"):
                candidate = str(result.get("candidate_name") or "").strip()
                score = float(result.get("candidate_score") or 0.0)
                if (
                    candidate
                    and candidate.lower() not in {"unknown", "face"}
                    and score >= soft_threshold
                ):
                    return True
        return False

    @staticmethod
    def _primary_unknown_face(results: list[dict[str, Any]]) -> bool:
        for result in results:
            if not result.get("primary", True):
                continue
            if not result.get("detection_valid", False):
                return False
            if not result.get("registration_eligible", False):
                return False
            box = result.get("box") or {}
            area = int(box.get("w", 0)) * int(box.get("h", 0))
            if area < 400:
                return False
            if result.get("recognized") or result.get("stabilized"):
                return False
            return True
        return False

    def _reset_locked(self) -> None:
        self._state = "idle"
        self._unknown_since = None
        self._unknown_streak = 0
        self._awaiting_since = 0.0
        self._listen_prompt_at = 0.0
        self._last_prompt_playback_seconds = 0.0
        self._voice_heard_since_listen = False
        self._no_speech_retries = 0
        self._pending_relisten_prompt = NAME_RETRY_PROMPT


def get_face_registration_service() -> FaceRegistrationService | None:
    return _service


def configure_face_registration(
    faces: Any,
    read_frame: Callable[[], np.ndarray | None],
) -> FaceRegistrationService:
    global _service
    _service = FaceRegistrationService(faces, read_frame)
    return _service
