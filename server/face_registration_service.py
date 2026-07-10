"""Automatic voice-triggered face registration (unknown face → prompt → name → capture)."""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Literal

import numpy as np

from esp_playback import esp_play_wav_url, post_wav_to_esp
from face_registration_voice import extract_registration_name
from tts_service import synthesize_sapi_wav_bytes
from wav_resample import resample_wav_bytes_to_mono_16bit

logger = logging.getLogger(__name__)

FaceRegState = Literal["idle", "awaiting_name", "capturing"]

REGISTRATION_PROMPT = (
    "I haven't registered your face yet. After the beep, please tell me your name."
)

VOICE_REG_TTS_RATE_HZ = 16000

_service: FaceRegistrationService | None = None


@dataclass
class FaceRegVoiceResult:
    handled: bool = False
    reply: str = ""
    registered_name: str | None = None


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

    for _ in range(samples):
        frame = read_frame()
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
        self.apply_settings_from_environ()

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

    @property
    def state(self) -> FaceRegState:
        with self._lock:
            return self._state

    def is_awaiting_name(self) -> bool:
        with self._lock:
            return self._state == "awaiting_name"

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
            }

    def on_frame(
        self,
        results: list[dict[str, Any]],
        *,
        voice_active: bool = False,
        vision_blocked: bool = False,
    ) -> None:
        """Called from MJPEG loop — may trigger proactive registration prompt."""
        if not self.enabled:
            return

        now = time.time()
        with self._lock:
            if self._state == "capturing":
                return
            if self._state == "awaiting_name":
                if voice_active:
                    self._awaiting_since = now
                elif (
                    self._awaiting_since > 0
                    and (now - self._awaiting_since) > self.await_name_seconds
                ):
                    logger.info("Face registration: awaiting_name timeout")
                    self._reset_locked()
                return

        if voice_active or vision_blocked:
            return

        if self._primary_recognized(results):
            with self._lock:
                self._unknown_since = None
            return

        if not self._primary_unknown_face(results):
            with self._lock:
                self._unknown_since = None
            return

        with self._lock:
            if self._unknown_since is None:
                self._unknown_since = now
                return
            stable_for = now - self._unknown_since
            if stable_for < self.unknown_seconds:
                return
            if (now - self._last_prompt_at) < self.prompt_cooldown_seconds:
                return

        self._try_prompt_registration()

    def handle_voice(self, user_text: str) -> FaceRegVoiceResult:
        with self._lock:
            if self._state != "awaiting_name":
                return FaceRegVoiceResult(handled=False)

        name = extract_registration_name(user_text)
        if not name:
            return FaceRegVoiceResult(
                handled=True,
                reply=(
                    "I didn't catch your name. Please say something like "
                    "my name is, and then your name."
                ),
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
        return FaceRegVoiceResult(
            handled=True,
            reply=f"All set, {name}. I've registered your face.",
            registered_name=name,
        )

    def _try_prompt_registration(self) -> None:
        if esp_play_wav_url() is None:
            logger.debug("Face registration prompt skipped: ESP_PLAY_WAV_URL not set")
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

        wav = self._synthesize_prompt_wav(REGISTRATION_PROMPT)
        try:
            post_wav_to_esp(wav, prompt_ack=True, prompt_ack_chime=True)
        except Exception as exc:
            logger.warning("Face registration prompt failed: %s", exc)
            with self._lock:
                self._reset_locked()
            return

        logger.info("Face registration prompt sent (awaiting name, beep then listen)")

    @staticmethod
    def _synthesize_prompt_wav(text: str) -> bytes:
        wav, _ = synthesize_sapi_wav_bytes(text.strip())
        return resample_wav_bytes_to_mono_16bit(wav, VOICE_REG_TTS_RATE_HZ)

    @staticmethod
    def _primary_recognized(results: list[dict[str, Any]]) -> bool:
        for result in results:
            if not result.get("primary", True):
                continue
            if result.get("recognized") or result.get("stabilized"):
                name = str(result.get("name", "")).strip().lower()
                if name and name not in {"unknown", "face"}:
                    return True
        return False

    @staticmethod
    def _primary_unknown_face(results: list[dict[str, Any]]) -> bool:
        for result in results:
            if not result.get("primary", True):
                continue
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
        self._awaiting_since = 0.0


def get_face_registration_service() -> FaceRegistrationService | None:
    return _service


def configure_face_registration(
    faces: Any,
    read_frame: Callable[[], np.ndarray | None],
) -> FaceRegistrationService:
    global _service
    _service = FaceRegistrationService(faces, read_frame)
    return _service
