"""Camera emotion on MJPEG frames — AWS Rekognition with local DrGM fallback."""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

EMOTION_AWS_LOCAL_DIR = Path(__file__).resolve().parent / "emotion-aws-local"


def _emotion_enabled() -> bool:
    return os.environ.get("EMOTION_ENABLED", "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _emotion_backend() -> str:
    return os.environ.get("EMOTION_BACKEND", "auto").strip().lower()


def _emotion_interval_s() -> float:
    return max(0.1, float(os.environ.get("EMOTION_INTERVAL_S", "0.5")))


def _box_iou(a: dict[str, int], b: tuple[int, int, int, int]) -> float:
    ax, ay, aw, ah = int(a["x"]), int(a["y"]), int(a["w"]), int(a["h"])
    bx, by, bw, bh = b
    x1 = max(ax, bx)
    y1 = max(ay, by)
    x2 = min(ax + aw, bx + bw)
    y2 = min(ay + ah, by + bh)
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    if inter <= 0:
        return 0.0
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def _format_emotion_label(emotion: str) -> str:
    key = str(emotion or "").strip().lower()
    if not key or key == "uncertain":
        return "Uncertain"
    return key.capitalize()


class EmotionService:
    """Attach emotion labels to face recognition results for the live feed."""

    def __init__(self) -> None:
        self._enabled = _emotion_enabled()
        self._backend_pref = _emotion_backend()
        self._interval_s = _emotion_interval_s()
        self._last_run_at = 0.0
        self._active_backend = "none"
        self._aws_client: Any | None = None
        self._aws_available = False
        self._local: Any | None = None
        self._local_available = False
        self._last_error = ""
        self._cached: dict[int, str] = {}
        self._init_backends()

    @property
    def available(self) -> bool:
        return self._enabled and (self._aws_available or self._local_available)

    def stats(self) -> dict[str, Any]:
        return {
            "enabled": self._enabled,
            "available": self.available,
            "backend": self._active_backend,
            "backend_preference": self._backend_pref,
            "aws_available": self._aws_available,
            "local_available": self._local_available,
            "interval_s": self._interval_s,
            "last_error": self._last_error,
        }

    def attach_emotions(self, frame_bgr: np.ndarray, results: list[dict[str, Any]]) -> None:
        """Mutate face results in place: result['emotion'] = display label."""
        if not self.available:
            return

        faces = [
            r
            for r in results
            if r.get("detection_valid", True) and isinstance(r.get("box"), dict)
        ]
        if not faces:
            self._cached.clear()
            return

        now = time.time()
        if now - self._last_run_at < self._interval_s:
            for idx, cached in self._cached.items():
                if 0 <= idx < len(faces) and cached:
                    faces[idx]["emotion"] = cached
            return

        self._last_run_at = now
        labels = self._infer_labels(frame_bgr, faces)
        self._cached = {idx: label for idx, label in enumerate(labels) if label}
        for idx, label in enumerate(labels):
            if label:
                faces[idx]["emotion"] = label

    def _infer_labels(self, frame_bgr: np.ndarray, faces: list[dict[str, Any]]) -> list[str]:
        backend = self._pick_backend()
        if backend == "aws":
            return self._infer_aws(frame_bgr, faces)
        if backend == "local":
            return self._infer_local(frame_bgr, faces)
        return [""] * len(faces)

    def _pick_backend(self) -> str:
        pref = self._backend_pref
        if pref == "aws":
            return "aws" if self._aws_available else ("local" if self._local_available else "none")
        if pref == "local":
            return "local" if self._local_available else ("aws" if self._aws_available else "none")
        # auto: AWS when configured, else local
        if self._aws_available:
            return "aws"
        if self._local_available:
            return "local"
        return "none"

    def _infer_aws(self, frame_bgr: np.ndarray, faces: list[dict[str, Any]]) -> list[str]:
        if self._aws_client is None:
            return [""] * len(faces)
        try:
            from rekognition_client import prepare_jpeg_bytes

            h, w = frame_bgr.shape[:2]
            jpeg = prepare_jpeg_bytes(frame_bgr)
            aws_faces = self._aws_client.detect_all_emotions(jpeg, (w, h))
            self._active_backend = "aws"
            self._last_error = ""

            labels = [""] * len(faces)
            used: set[int] = set()
            for face_idx, result in enumerate(faces):
                box = result["box"]
                best_idx = -1
                best_iou = 0.35
                for i, aws_face in enumerate(aws_faces):
                    if i in used:
                        continue
                    iou = _box_iou(box, aws_face.face_box)
                    if iou > best_iou:
                        best_iou = iou
                        best_idx = i
                if best_idx >= 0:
                    used.add(best_idx)
                    labels[face_idx] = _format_emotion_label(aws_faces[best_idx].project_emotion)
            return labels
        except Exception as exc:
            self._last_error = str(exc)
            logger.warning("AWS emotion failed (%s); trying local fallback", exc)
            if self._local_available:
                return self._infer_local(frame_bgr, faces)
            return [""] * len(faces)

    def _infer_local(self, frame_bgr: np.ndarray, faces: list[dict[str, Any]]) -> list[str]:
        if self._local is None:
            return [""] * len(faces)
        try:
            if not self._local.available:
                self._local.load()
            self._active_backend = "local"
            self._last_error = ""
            labels: list[str] = []
            for result in faces:
                crop = self._local.square_padded_crop(frame_bgr, result["box"])
                if crop is None:
                    labels.append("")
                    continue
                pred = self._local.predict_crop(crop)
                labels.append(_format_emotion_label(pred.emotion) if pred else "")
            return labels
        except Exception as exc:
            self._last_error = str(exc)
            logger.warning("Local emotion failed: %s", exc)
            return [""] * len(faces)

    def _init_backends(self) -> None:
        if not self._enabled:
            return

        aws_path = str(EMOTION_AWS_LOCAL_DIR)
        if aws_path not in sys.path:
            sys.path.insert(0, aws_path)

        if self._backend_pref in {"auto", "aws"}:
            self._try_init_aws()
        if self._backend_pref in {"auto", "local"} or not self._aws_available:
            self._try_init_local(eager=not self._aws_available or self._backend_pref == "local")

        if self.available:
            logger.info(
                "Emotion service ready (pref=%s, aws=%s, local=%s)",
                self._backend_pref,
                self._aws_available,
                self._local_available,
            )
        else:
            logger.warning("Emotion service unavailable (no AWS credentials and no local model)")

    def _try_init_aws(self) -> None:
        try:
            from aws_config import get_aws_config
            from rekognition_client import RekognitionEmotionClient

            config = get_aws_config()
            self._aws_client = RekognitionEmotionClient(config)
            self._aws_available = True
            self._active_backend = "aws"
            logger.info("AWS Rekognition emotion ready (region=%s)", config.region)
        except ImportError:
            logger.info("boto3 not installed — AWS emotion disabled")
        except Exception as exc:
            logger.info("AWS emotion not configured: %s", exc)

    def _try_init_local(self, *, eager: bool = False) -> None:
        try:
            from local_recognizer import MODEL_DIR, LocalEmotionRecognizer

            if not (MODEL_DIR / "config.json").is_file():
                return
            if not (MODEL_DIR / "model.safetensors").is_file():
                return

            recognizer = LocalEmotionRecognizer()
            if eager:
                recognizer.load()
            self._local = recognizer
            self._local_available = True
            if not self._aws_available:
                self._active_backend = "local"
            logger.info(
                "Local DrGM emotion model %s",
                "ready" if eager else "available (loads on first use)",
            )
        except ImportError as exc:
            logger.info("Local emotion deps missing (torch/transformers): %s", exc)
        except Exception as exc:
            logger.info("Local emotion model not loaded: %s", exc)
