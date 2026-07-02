"""Vision emotion pipeline (P1): stabilize CNN on primary face → LLM empathy → TTS + eye tag."""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from emotion_service import EMOTION_SPOKEN, EMOTION_TO_EYE, EmotionService, SPEAKABLE_EMOTIONS
from eye_expression import normalize_eye_expression
from pipeline_priority import vision_emotion_blocked

logger = logging.getLogger(__name__)


def _vision_emotion_enabled() -> bool:
    return os.environ.get("VISION_EMOTION_ENABLED", "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


@dataclass(frozen=True)
class _EmpathyJob:
    person_name: str
    emotion_label: str
    emotion_spoken: str
    eye_expression: str | None
    confidence: float


@dataclass
class _LockedDisplay:
    name: str
    emotion: str | None
    confidence: float | None
    raw_emotion: str | None
    scores: dict[str, float] | None


class VisionEmotionService:
    """Accumulate emotion on the primary recognized face for ~2–2.5 s, then speak."""

    def __init__(
        self,
        emotion: EmotionService,
        *,
        speak_wav: Callable[[str, str | None], None],
        is_speaker_busy: Callable[[], bool] | None = None,
        defer_empathy_for: Callable[[str], bool] | None = None,
    ) -> None:
        self._emotion = emotion
        self._speak_wav = speak_wav
        self._is_speaker_busy = is_speaker_busy or (lambda: False)
        self._defer_empathy_for = defer_empathy_for

        self._window_min_s = float(os.environ.get("VISION_EMOTION_WINDOW_MIN_S", "2.0"))
        self._window_max_s = float(os.environ.get("VISION_EMOTION_WINDOW_MAX_S", "2.5"))
        self._cooldown_s = float(os.environ.get("VISION_EMOTION_COOLDOWN_S", "120"))
        self._dominance_ratio = float(os.environ.get("VISION_EMOTION_DOMINANCE", "0.35"))
        self._display_sample_s = float(os.environ.get("VISION_EMOTION_DISPLAY_SAMPLE_S", "1.0"))
        self._display_hold_s = float(os.environ.get("VISION_EMOTION_DISPLAY_HOLD_S", "5.0"))

        self._lock = threading.Lock()
        self._accum_person: str | None = None
        self._accum_started_at: float = 0.0
        self._accum_votes: dict[str, int] = {}
        self._accum_conf_sum: dict[str, float] = {}
        self._accum_frames = 0
        self._last_spoken_at: dict[str, float] = {}
        self._summary_greet_suppress_until: dict[str, float] = {}
        self._last_error = ""
        self._latest_overlay: dict[str, Any] | None = None
        self._pending_jobs: list[_EmpathyJob] = []
        self._inflight_person: str | None = None

        self._display_person: str | None = None
        self._display_phase: str = "sample"
        self._display_phase_started_at: float = 0.0
        self._display_sample_votes: dict[str, int] = {}
        self._display_sample_conf_sum: dict[str, float] = {}
        self._display_sample_raw: dict[str, int] = {}
        self._display_sample_scores_sum: dict[str, dict[str, float]] = {}
        self._display_locked: _LockedDisplay | None = None

        self._worker = threading.Thread(
            target=self._run_worker, name="vision-emotion-worker", daemon=True
        )
        self._worker.start()

    def enabled(self) -> bool:
        return _vision_emotion_enabled() and self._emotion.available

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self.enabled(),
                "accumulating_for": self._accum_person,
                "accum_frames": self._accum_frames,
                "window_min_s": self._window_min_s,
                "window_max_s": self._window_max_s,
                "cooldown_s": self._cooldown_s,
                "display_sample_s": self._display_sample_s,
                "display_hold_s": self._display_hold_s,
                "display_phase": self._display_phase,
                "display_locked_emotion": (
                    self._display_locked.emotion if self._display_locked else None
                ),
                "latest_overlay": dict(self._latest_overlay or {}),
                "pending_jobs": len(self._pending_jobs),
                "last_error": self._last_error,
                "emotion_model": self._emotion.stats(),
            }

    def latest_overlay(self) -> dict[str, Any] | None:
        with self._lock:
            return dict(self._latest_overlay) if self._latest_overlay else None

    def notify_summary_greeting_spoken(self, person_name: str) -> None:
        """Pause empathy after the Phase C startup greeting so the user hears one line."""
        name = str(person_name or "").strip()
        if not name or name.lower() in {"unknown", "face"}:
            return
        pause_s = float(os.environ.get("VISION_EMOTION_AFTER_SUMMARY_GREETING_S", "180"))
        until = time.time() + max(30.0, pause_s)
        with self._lock:
            self._summary_greet_suppress_until[name] = until
            self._last_spoken_at[name] = time.time()
            self._pending_jobs = [j for j in self._pending_jobs if j.person_name != name]
            self._reset_accum_locked()
            self._reset_display_hold_locked()
        logger.info(
            "Vision empathy paused %.0fs after summary greeting for %s",
            pause_s,
            name,
        )

    def _empathy_suppressed(self, name: str) -> bool:
        now = time.time()
        with self._lock:
            if self._summary_greet_suppress_until.get(name, 0.0) > now:
                return True
        if self._defer_empathy_for and self._defer_empathy_for(name):
            return True
        return False

    def process_frame(self, frame: Any, results: list[dict[str, Any]]) -> None:
        """Called from the MJPEG loop on every frame."""
        if not self.enabled():
            return

        primary = _primary_face_for_emotion(results)
        if primary is None:
            self._reset_accum_locked()
            with self._lock:
                self._reset_display_hold_locked()
                self._latest_overlay = None
            return

        name = str(primary.get("emotion_name") or primary.get("name") or "").strip()
        box = primary.get("box") or {}
        if not name or name.lower() in {"unknown", "face"}:
            self._reset_accum_locked()
            with self._lock:
                self._reset_display_hold_locked()
                self._latest_overlay = None
            return

        overlay_info = self._emotion.detect_overlay(frame, box)
        now = time.time()
        with self._lock:
            overlay = self._update_display_hold_locked(name, overlay_info, now)

        empathy_paused = (
            vision_emotion_blocked()
            or self._is_speaker_busy()
            or self._empathy_suppressed(name)
        )
        if empathy_paused:
            with self._lock:
                if self._pending_jobs:
                    self._pending_jobs = [
                        j for j in self._pending_jobs if j.person_name != name
                    ]
                self._reset_accum_locked()
                if vision_emotion_blocked() or self._is_speaker_busy():
                    overlay["deferred"] = "voice_or_tts_busy"
                else:
                    overlay["deferred"] = "startup_greeting"
                self._latest_overlay = overlay
            return

        detected = self._emotion.detect(frame, box)
        with self._lock:
            if self._accum_person != name:
                self._accum_person = name
                self._accum_started_at = now
                self._accum_votes = {}
                self._accum_conf_sum = {}
                self._accum_frames = 0

            self._accum_frames += 1
            elapsed = now - self._accum_started_at
            overlay["accum_s"] = round(elapsed, 2)

            if detected and detected.get("speakable"):
                label = str(detected["label"])
                self._accum_votes[label] = self._accum_votes.get(label, 0) + 1
                self._accum_conf_sum[label] = self._accum_conf_sum.get(label, 0.0) + float(
                    detected["confidence"]
                )
            elif overlay_info and overlay_info.get("speakable"):
                # Vote from overlay path when detect() gate is strict.
                label = str(overlay_info["label"])
                self._accum_votes[label] = self._accum_votes.get(label, 0) + 1
                self._accum_conf_sum[label] = self._accum_conf_sum.get(label, 0.0) + float(
                    overlay_info["confidence"]
                )

            self._latest_overlay = overlay

            if elapsed < self._window_min_s:
                return

            ready = elapsed >= self._window_max_s or (
                elapsed >= self._window_min_s and self._dominant_emotion_locked() is not None
            )
            if not ready:
                return

            dominant = self._dominant_emotion_locked()
            if dominant is None:
                return

            last = self._last_spoken_at.get(name, 0.0)
            if last > 0.0 and (now - last) < self._cooldown_s:
                return

            if self._inflight_person == name or any(
                j.person_name == name for j in self._pending_jobs
            ):
                return

            label, _vote_count, avg_conf = dominant
            if label not in SPEAKABLE_EMOTIONS:
                return

            eye = normalize_eye_expression(EMOTION_TO_EYE.get(label))
            spoken = EMOTION_SPOKEN.get(label, label)
            self._pending_jobs.append(
                _EmpathyJob(
                    person_name=name,
                    emotion_label=label,
                    emotion_spoken=spoken,
                    eye_expression=eye,
                    confidence=avg_conf,
                )
            )
            self._reset_accum_locked()

    def _dominant_emotion_locked(self) -> tuple[str, int, float] | None:
        if not self._accum_votes or self._accum_frames <= 0:
            return None
        label, votes = max(self._accum_votes.items(), key=lambda item: item[1])
        ratio = votes / float(self._accum_frames)
        if ratio < self._dominance_ratio:
            return None
        avg_conf = self._accum_conf_sum.get(label, 0.0) / max(1, votes)
        return label, votes, avg_conf

    def _reset_display_hold_locked(self) -> None:
        self._display_person = None
        self._display_phase = "sample"
        self._display_phase_started_at = 0.0
        self._display_sample_votes = {}
        self._display_sample_conf_sum = {}
        self._display_sample_raw = {}
        self._display_sample_scores_sum = {}
        self._display_locked = None

    def _start_display_sample_locked(self, name: str, now: float) -> None:
        self._display_person = name
        self._display_phase = "sample"
        self._display_phase_started_at = now
        self._display_sample_votes = {}
        self._display_sample_conf_sum = {}
        self._display_sample_raw = {}
        self._display_sample_scores_sum = {}

    def _record_display_sample_locked(self, overlay_info: dict[str, Any] | None) -> None:
        if not overlay_info:
            return
        label = str(overlay_info.get("label") or "")
        if not label:
            return
        self._display_sample_votes[label] = self._display_sample_votes.get(label, 0) + 1
        self._display_sample_conf_sum[label] = self._display_sample_conf_sum.get(label, 0.0) + float(
            overlay_info.get("confidence") or 0.0
        )
        raw = str(overlay_info.get("raw_label") or label)
        self._display_sample_raw[raw] = self._display_sample_raw.get(raw, 0) + 1
        scores = overlay_info.get("scores") or {}
        if isinstance(scores, dict):
            bucket = self._display_sample_scores_sum.setdefault(label, {})
            for key, value in scores.items():
                bucket[str(key)] = bucket.get(str(key), 0.0) + float(value)

    def _finalize_display_sample_locked(self, name: str) -> _LockedDisplay:
        if not self._display_sample_votes:
            prev = self._display_locked
            if prev and prev.name == name:
                return prev
            return _LockedDisplay(
                name=name,
                emotion=None,
                confidence=None,
                raw_emotion=None,
                scores=None,
            )

        label = max(self._display_sample_votes.items(), key=lambda item: item[1])[0]
        votes = self._display_sample_votes[label]
        confidence = self._display_sample_conf_sum.get(label, 0.0) / max(1, votes)
        raw_emotion = None
        if self._display_sample_raw:
            raw_emotion = max(self._display_sample_raw.items(), key=lambda item: item[1])[0]
        scores_bucket = self._display_sample_scores_sum.get(label) or {}
        scores = (
            {k: round(v / max(1, votes), 3) for k, v in scores_bucket.items()}
            if scores_bucket
            else None
        )
        return _LockedDisplay(
            name=name,
            emotion=label,
            confidence=round(confidence, 3),
            raw_emotion=raw_emotion,
            scores=scores,
        )

    def _overlay_from_locked_locked(self, now: float) -> dict[str, Any]:
        locked = self._display_locked
        if locked is None:
            return {"name": self._display_person or "", "emotion": None, "accum_s": 0.0}

        phase_elapsed = now - self._display_phase_started_at
        if self._display_phase == "sample":
            accum_s = round(min(phase_elapsed, self._display_sample_s), 2)
            phase = "sampling"
        else:
            accum_s = round(min(phase_elapsed, self._display_hold_s), 2)
            phase = "holding"

        return {
            "name": locked.name,
            "emotion": locked.emotion,
            "confidence": locked.confidence,
            "raw_emotion": locked.raw_emotion,
            "scores": locked.scores,
            "accum_s": accum_s,
            "display_phase": phase,
        }

    def _update_display_hold_locked(
        self,
        name: str,
        overlay_info: dict[str, Any] | None,
        now: float,
    ) -> dict[str, Any]:
        if self._display_person != name:
            self._start_display_sample_locked(name, now)

        phase_elapsed = now - self._display_phase_started_at

        if self._display_phase == "sample":
            self._record_display_sample_locked(overlay_info)
            if phase_elapsed >= self._display_sample_s:
                self._display_locked = self._finalize_display_sample_locked(name)
                self._display_phase = "hold"
                self._display_phase_started_at = now
        elif phase_elapsed >= self._display_hold_s:
            self._start_display_sample_locked(name, now)
            self._record_display_sample_locked(overlay_info)

        overlay = self._overlay_from_locked_locked(now)
        if self._display_phase == "sample" and self._display_locked is None and overlay_info:
            overlay = {
                **overlay,
                "name": name,
                "emotion": overlay_info.get("label"),
                "confidence": overlay_info.get("confidence"),
                "raw_emotion": overlay_info.get("raw_label"),
                "scores": overlay_info.get("scores"),
            }
        self._latest_overlay = overlay
        return overlay

    def _reset_accum_locked(self) -> None:
        self._accum_person = None
        self._accum_started_at = 0.0
        self._accum_votes = {}
        self._accum_conf_sum = {}
        self._accum_frames = 0

    def _reset_accum(self) -> None:
        with self._lock:
            self._reset_accum_locked()

    def _run_worker(self) -> None:
        while True:
            job: _EmpathyJob | None = None
            with self._lock:
                if self._pending_jobs:
                    job = self._pending_jobs.pop(0)
                    self._inflight_person = job.person_name

            if job is None:
                time.sleep(0.05)
                continue

            now = time.time()
            with self._lock:
                if self._summary_greet_suppress_until.get(job.person_name, 0.0) > now:
                    logger.info(
                        "Vision empathy skipped (post-summary cooldown) for %s",
                        job.person_name,
                    )
                    self._inflight_person = None
                    continue

            if self._defer_empathy_for and self._defer_empathy_for(job.person_name):
                logger.info(
                    "Vision empathy deferred (startup greeting pending) for %s",
                    job.person_name,
                )
                with self._lock:
                    self._pending_jobs.insert(0, job)
                    self._inflight_person = None
                time.sleep(0.2)
                continue

            if vision_emotion_blocked() or self._is_speaker_busy():
                logger.info(
                    "Vision empathy deferred (voice/busy) for %s emotion=%s",
                    job.person_name,
                    job.emotion_label,
                )
                with self._lock:
                    self._pending_jobs.insert(0, job)
                    self._inflight_person = None
                time.sleep(0.2)
                continue

            try:
                from llm_service import empathy_for_detected_emotion

                logger.info(
                    "Vision emotion: person=%s emotion=%s (avg_conf=%.2f) eye=%s",
                    job.person_name,
                    job.emotion_label,
                    job.confidence,
                    job.eye_expression or "idle",
                )
                text = empathy_for_detected_emotion(
                    job.person_name,
                    job.emotion_spoken,
                    emotion_label=job.emotion_label,
                )
                if not text:
                    raise RuntimeError("LLM returned empty empathy reply")

                self._speak_wav(text, job.eye_expression)
                with self._lock:
                    self._last_spoken_at[job.person_name] = time.time()
                    self._last_error = ""
            except Exception as exc:
                logger.exception("Vision empathy failed for %s", job.person_name)
                with self._lock:
                    self._last_error = str(exc)[:240]
            finally:
                with self._lock:
                    self._inflight_person = None
            time.sleep(0.05)


def _primary_face_for_emotion(results: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Largest primary face with a usable identity (stabilized, recognized, or strong candidate)."""
    best: dict[str, Any] | None = None
    best_area = 0
    soft_threshold = float(os.environ.get("FACE_MATCH_SOFT_THRESHOLD", "0.39"))

    for result in results:
        if not result.get("primary", True):
            continue

        name = ""
        if result.get("stabilized") or result.get("recognized"):
            name = str(result.get("name") or "").strip()
        if not name or name.lower() in {"unknown", "face"}:
            candidate = str(result.get("candidate_name") or "").strip()
            score = float(result.get("candidate_score") or 0.0)
            if candidate and score >= soft_threshold:
                name = candidate

        if not name or name.lower() in {"unknown", "face"}:
            continue

        box = result.get("box") or {}
        area = int(box.get("w", 0)) * int(box.get("h", 0))
        if area > best_area:
            best_area = area
            enriched = dict(result)
            enriched["emotion_name"] = name
            best = enriched
    return best
