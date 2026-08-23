"""Speaker embedding store and recognition (ECAPA-TDNN via SpeechBrain).

Mirrors ``FaceService``: enrollment WAVs under ``data/voices/<person_id>/`` and
embeddings in ``data/voice_embeddings.json``.  Used with face recognition for
cross-modal speaker identity during voice sessions.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import threading
import time
import unicodedata
import wave
from pathlib import Path
from typing import Any, Literal

import numpy as np

logger = logging.getLogger(__name__)

VoiceMatchState = Literal["recognized", "unknown", "no_voice"]

SPEECHBRAIN_MODEL = "speechbrain/spkrec-ecapa-voxceleb"


def _person_id(name: str) -> str:
    cleaned = unicodedata.normalize("NFKC", (name or "").strip().lower())
    cleaned = re.sub(r"[^\w\-]+", "_", cleaned, flags=re.UNICODE)
    return cleaned.strip("_")


def _wav_to_float_mono(wav_bytes: bytes, target_rate: int = 16_000) -> np.ndarray | None:
    """Decode PCM WAV to mono float32 at ``target_rate``."""
    if not wav_bytes:
        return None
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            channels = wf.getnchannels()
            sample_width = wf.getsampwidth()
            rate = wf.getframerate()
            frames = wf.readframes(wf.getnframes())
    except Exception:
        return None
    if sample_width != 2 or rate <= 0 or not frames:
        return None
    pcm = np.frombuffer(frames, dtype=np.int16)
    if channels > 1:
        pcm = pcm.reshape(-1, channels).mean(axis=1).astype(np.int16)
    samples = pcm.astype(np.float32) / 32768.0
    if len(samples) < int(rate * 0.35):
        return None
    if rate != target_rate:
        duration = len(samples) / float(rate)
        target_n = max(1, int(duration * target_rate))
        x_old = np.linspace(0.0, 1.0, num=len(samples), endpoint=False)
        x_new = np.linspace(0.0, 1.0, num=target_n, endpoint=False)
        samples = np.interp(x_new, x_old, samples).astype(np.float32)
    return samples


class VoiceProfileService:
    """Lazy-loaded speaker encoder with persistent embedding store."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.voices_dir = data_dir / "voices"
        self.embeddings_path = data_dir / "voice_embeddings.json"
        self._lock = threading.Lock()
        self._model_lock = threading.RLock()
        self._encoder: Any | None = None
        self._encoder_failed = False
        # person_id -> (display_name, embeddings ndarray [n, dim])
        self._embeddings: dict[str, tuple[str, np.ndarray]] = {}
        self.apply_settings_from_environ()
        self.voices_dir.mkdir(parents=True, exist_ok=True)
        self._load_embeddings()

    def apply_settings_from_environ(self) -> None:
        self.enabled = os.environ.get("VOICE_PROFILE_ENABLED", "1").strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }
        self.match_threshold = float(os.environ.get("VOICE_MATCH_THRESHOLD", "0.72"))
        self.match_soft_threshold = float(
            os.environ.get(
                "VOICE_MATCH_SOFT_THRESHOLD",
                f"{max(0.55, self.match_threshold - 0.06):.3f}",
            )
        )
        self.margin_min = float(os.environ.get("VOICE_MATCH_MARGIN_MIN", "0.06"))
        self.min_enroll_samples = max(
            1, int(os.environ.get("VOICE_ENROLL_MIN_SAMPLES", "3"))
        )
        self.max_enroll_samples = max(
            self.min_enroll_samples,
            int(os.environ.get("VOICE_ENROLL_MAX_SAMPLES", "8")),
        )
        self.min_audio_seconds = float(os.environ.get("VOICE_MIN_AUDIO_SECONDS", "0.6"))

    @property
    def model_ready(self) -> bool:
        return self._encoder is not None and not self._encoder_failed

    def _ensure_encoder(self) -> bool:
        if self._encoder_failed:
            return False
        if self._encoder is not None:
            return True
        with self._model_lock:
            if self._encoder is not None:
                return True
            if self._encoder_failed:
                return False
            try:
                import torch
                from speechbrain.inference.speaker import EncoderClassifier

                run_opts = (
                    {"device": "cuda:0"}
                    if torch.cuda.is_available()
                    else {"device": "cpu"}
                )
                self._encoder = EncoderClassifier.from_hparams(
                    source=SPEECHBRAIN_MODEL,
                    savedir=str(self.data_dir / "models" / "spkrec-ecapa-voxceleb"),
                    run_opts=run_opts,
                )
                logger.info(
                    "Voice profile encoder ready (%s)",
                    run_opts.get("device", "cpu"),
                )
                return True
            except Exception as exc:
                self._encoder_failed = True
                logger.warning("Voice profile encoder unavailable: %s", exc)
                return False

    def _load_embeddings(self) -> None:
        if not self.embeddings_path.is_file():
            return
        try:
            raw = json.loads(self.embeddings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not load voice embeddings: %s", exc)
            return
        people = raw.get("people") or {}
        loaded: dict[str, tuple[str, np.ndarray]] = {}
        for person_id, entry in people.items():
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or person_id).strip()
            vectors = entry.get("embeddings") or []
            arr = np.asarray(vectors, dtype=np.float32)
            if arr.ndim == 1:
                arr = arr.reshape(1, -1)
            if arr.size == 0:
                continue
            norms = np.linalg.norm(arr, axis=1, keepdims=True)
            norms = np.maximum(norms, 1e-8)
            arr = arr / norms
            loaded[str(person_id)] = (name, arr)
        with self._lock:
            self._embeddings = loaded

    def persist_embeddings(self) -> None:
        with self._lock:
            people: dict[str, Any] = {}
            for person_id, (display_name, embs) in self._embeddings.items():
                people[person_id] = {
                    "name": display_name,
                    "embeddings": embs.tolist(),
                }
            payload = {
                "version": 1,
                "model": SPEECHBRAIN_MODEL,
                "people": people,
            }
            tmp = self.embeddings_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(self.embeddings_path)

    def list_people(self) -> list[str]:
        with self._lock:
            return sorted({name for name, _ in self._embeddings.values()})

    def sample_count(self, name: str) -> int:
        pid = _person_id(name)
        with self._lock:
            entry = self._embeddings.get(pid)
            if entry is None:
                return 0
            return int(entry[1].shape[0])

    def needs_enrollment(self, name: str) -> bool:
        if not self.enabled or not name or not name.strip():
            return False
        return self.sample_count(name) < self.min_enroll_samples

    def same_person(self, name_a: str, name_b: str) -> bool:
        a = _person_id(name_a)
        b = _person_id(name_b)
        return bool(a) and a == b

    def embed_wav(self, wav_bytes: bytes) -> np.ndarray | None:
        if not self.enabled:
            return None
        samples = _wav_to_float_mono(wav_bytes)
        if samples is None:
            return None
        if len(samples) < int(16_000 * self.min_audio_seconds):
            return None
        if not self._ensure_encoder():
            return None
        try:
            import torch

            tensor = torch.from_numpy(samples).unsqueeze(0)
            with self._model_lock:
                assert self._encoder is not None
                embedding = self._encoder.encode_batch(tensor)
            vec = embedding.squeeze().detach().cpu().numpy().astype(np.float32)
            norm = float(np.linalg.norm(vec))
            if norm <= 1e-8:
                return None
            return vec / norm
        except Exception:
            logger.debug("Voice embed failed", exc_info=True)
            return None

    @staticmethod
    def _person_match_score(embs: np.ndarray, query: np.ndarray) -> float:
        if embs.size == 0:
            return -1.0
        sims = embs @ query
        top_mean = float(np.mean(np.sort(sims)[-min(3, len(sims)):]))
        centroid = embs.mean(axis=0)
        cn = float(np.linalg.norm(centroid))
        if cn <= 0.0:
            return top_mean
        centroid /= cn
        return 0.45 * top_mean + 0.55 * float(centroid @ query)

    def _match_embedding(self, embedding: np.ndarray) -> tuple[str | None, float, float]:
        best_name: str | None = None
        best_score = -1.0
        second_best = -1.0
        with self._lock:
            for _pid, (display_name, embs) in self._embeddings.items():
                if embs.size == 0:
                    continue
                score = self._person_match_score(embs, embedding)
                if score > best_score:
                    second_best = best_score
                    best_score = score
                    best_name = display_name
                elif score > second_best:
                    second_best = score
        return best_name, best_score, second_best

    def recognize_speaker(self, wav_bytes: bytes) -> tuple[str | None, float, VoiceMatchState]:
        if not self.enabled:
            return None, 0.0, "no_voice"
        embedding = self.embed_wav(wav_bytes)
        if embedding is None:
            return None, 0.0, "no_voice"
        with self._lock:
            if not self._embeddings:
                return None, 0.0, "unknown"
        best_name, best_score, second_best = self._match_embedding(embedding)
        margin = best_score - max(second_best, -1.0)
        if (
            best_name
            and best_score >= self.match_threshold
            and margin >= self.margin_min
        ):
            return best_name, best_score, "recognized"
        if best_name and best_score >= self.match_soft_threshold:
            return best_name, best_score, "unknown"
        return None, best_score, "unknown"

    def register_sample(self, name: str, wav_bytes: bytes) -> bool:
        """Append one enrollment sample for ``name``; returns True when saved."""
        cleaned = (name or "").strip()
        if not cleaned or cleaned.lower().startswith("guest"):
            return False
        embedding = self.embed_wav(wav_bytes)
        if embedding is None:
            return False
        pid = _person_id(cleaned)
        person_dir = self.voices_dir / pid
        person_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        wav_path = person_dir / f"{stamp}.wav"
        try:
            wav_path.write_bytes(wav_bytes)
        except OSError:
            logger.debug("Could not save voice sample wav", exc_info=True)

        with self._lock:
            display, embs = self._embeddings.get(pid, (cleaned, np.empty((0, embedding.size))))
            if embs.size == 0:
                merged = embedding.reshape(1, -1)
            else:
                merged = np.vstack([embs, embedding.reshape(1, -1)])
            if merged.shape[0] > self.max_enroll_samples:
                merged = merged[-self.max_enroll_samples :]
            self._embeddings[pid] = (display or cleaned, merged)
        self.persist_embeddings()
        logger.info(
            "Voice profile sample saved name=%s count=%d",
            cleaned,
            self.sample_count(cleaned),
        )
        return True

    def enroll_registration(self, name: str, wav_bytes: bytes) -> int:
        """Save one registration utterance toward the voice profile."""
        if not self.enabled:
            return 0
        return 1 if self.register_sample(name, wav_bytes) else 0

    def maybe_enroll_turn(self, name: str, wav_bytes: bytes) -> bool:
        """Collect early-session samples until the profile is complete."""
        if not self.needs_enrollment(name):
            return False
        return self.register_sample(name, wav_bytes)
