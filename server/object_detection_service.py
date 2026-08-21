"""Camera object detection on MJPEG frames — Ultralytics YOLO26n."""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np

logger = logging.getLogger(__name__)

MODEL_DIR = Path(__file__).resolve().parent / "data" / "models"
DEFAULT_MODEL = "yolo26n.pt"

_TRUTHY = {"1", "true", "yes", "on"}

# Retry a failed model load at most this often so a missing/corrupt weight file
# cannot turn the vision loop into a download-retry hot path.
_LOAD_RETRY_SECONDS = 60.0

_IRREGULAR_PLURALS = {
    "person": "people",
    "mouse": "mice",
    "knife": "knives",
    "sandwich": "sandwiches",
    "couch": "couches",
    "bench": "benches",
    "bus": "buses",
    "wine glass": "wine glasses",
    "toothbrush": "toothbrushes",
    "broccoli": "broccoli",
    "scissors": "scissors",
    "skis": "skis",
}

_BOX_PALETTE = (
    (255, 128, 0),
    (0, 215, 255),
    (255, 0, 200),
    (80, 220, 100),
    (0, 165, 255),
    (200, 120, 255),
    (255, 200, 60),
    (120, 255, 220),
)


def _weights_dir() -> Path:
    """Directory to cache YOLO weights in.

    Ultralytics strips apostrophes out of every weights path it is given, so if
    the project lives under a directory containing one it never finds the cached
    file and re-downloads on every load. Fall back to a cache outside the project
    when that applies.
    """
    preferred = MODEL_DIR
    if "'" not in str(preferred):
        return preferred

    for fallback in (
        Path.home() / ".cache" / "nino" / "models",
        Path(tempfile.gettempdir()) / "nino-models",
    ):
        if "'" not in str(fallback):
            logger.info(
                "Caching YOLO weights in %s — %s contains an apostrophe, which "
                "Ultralytics strips from weights paths",
                fallback,
                preferred,
            )
            return fallback
    return preferred


def _env_flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() in _TRUTHY


def _detection_enabled() -> bool:
    return _env_flag("OBJECT_DETECTION_ENABLED")


def _model_name() -> str:
    return os.environ.get("OBJECT_DETECTION_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL


def _detection_interval_s() -> float:
    return max(0.05, float(os.environ.get("OBJECT_DETECTION_INTERVAL_S", "0.5")))


def _detection_confidence() -> float:
    return min(0.95, max(0.05, float(os.environ.get("OBJECT_DETECTION_CONFIDENCE", "0.35"))))


def _detection_imgsz() -> int:
    return max(160, int(os.environ.get("OBJECT_DETECTION_IMGSZ", "320")))


def _max_objects() -> int:
    return max(1, int(os.environ.get("OBJECT_DETECTION_MAX_OBJECTS", "12")))


def _class_allowlist() -> list[str]:
    raw = os.environ.get("OBJECT_DETECTION_CLASSES", "").strip()
    if not raw:
        return []
    return [part.strip().lower() for part in raw.split(",") if part.strip()]


def _pluralize(label: str, count: int) -> str:
    if count <= 1:
        return label
    if label in _IRREGULAR_PLURALS:
        return _IRREGULAR_PLURALS[label]
    if label.endswith(("s", "x", "z", "ch", "sh")):
        return f"{label}es"
    return f"{label}s"


def _with_article(label: str) -> str:
    return f"{'an' if label[:1].lower() in 'aeiou' else 'a'} {label}"


def _class_color(class_id: int) -> tuple[int, int, int]:
    return _BOX_PALETTE[class_id % len(_BOX_PALETTE)]


def summarize_detections(detections: list[dict[str, Any]]) -> str:
    """Natural-language scene phrase, e.g. "2 people, a laptop and a cup"."""
    counts: dict[str, int] = {}
    for det in detections:
        label = str(det.get("label", "")).strip().lower()
        if label:
            counts[label] = counts.get(label, 0) + 1
    if not counts:
        return ""

    phrases = [
        f"{count} {_pluralize(label, count)}" if count > 1 else _with_article(label)
        for label, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    if len(phrases) == 1:
        return phrases[0]
    return f"{', '.join(phrases[:-1])} and {phrases[-1]}"


_PERSON_LABELS = {"person", "people"}
EMPTY_SCENE_NOTE = "I don't see anyone or anything distinctive"


def join_visible_names(names: list[str] | None) -> str:
    """'Hari', 'Hari and Nora', or 'Hari, Nora and Sam'."""
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in names or []:
        name = str(raw or "").strip()
        key = name.lower()
        if not name or key in {"unknown", "face"} or key in seen:
            continue
        seen.add(key)
        cleaned.append(name)
    if not cleaned:
        return ""
    if len(cleaned) == 1:
        return cleaned[0]
    if len(cleaned) == 2:
        return f"{cleaned[0]} and {cleaned[1]}"
    return f"{', '.join(cleaned[:-1])} and {cleaned[-1]}"


def phrase_visible_scene(
    names: list[str] | None,
    detections: list[dict[str, Any]] | None,
) -> str:
    """People + objects together, e.g. 'Hari and a laptop'.

    Registered names suppress YOLO 'person' counts so we do not say
    'Hari and a person'.
    """
    people = join_visible_names(names)
    dets = list(detections or [])
    if people:
        dets = [
            det
            for det in dets
            if str(det.get("label", "")).strip().lower() not in _PERSON_LABELS
        ]
    objects = summarize_detections(dets)
    if people and objects:
        return f"{people} and {objects}"
    return people or objects or ""


def spoken_scene_report(
    names: list[str] | None,
    detections: list[dict[str, Any]] | None,
    *,
    pose: str = "center",
) -> str:
    """Full spoken line for a look-scan pose."""
    content = phrase_visible_scene(names, detections)
    side = (pose or "center").strip().lower()
    if side == "left":
        if not content:
            return f"On my left {EMPTY_SCENE_NOTE}."
        return f"On my left I see {content}."
    if side == "right":
        if not content:
            return f"On my right {EMPTY_SCENE_NOTE}."
        return f"On my right I see {content}."
    if not content:
        return f"{EMPTY_SCENE_NOTE}."
    if side == "front":
        return f"Right in front of me I see {content}."
    return f"Right now I see {content}."


class ObjectDetectionService:
    """Detect COCO objects in camera frames with Ultralytics YOLO26n.

    Inference is throttled to one pass per interval and the last result is
    replayed on intermediate frames, so the MJPEG stream and the background
    vision loop can both ask for detections on every frame they handle.
    """

    def __init__(self) -> None:
        self._enabled = _detection_enabled()
        self._model_name = _model_name()
        self._interval_s = _detection_interval_s()
        self._confidence = _detection_confidence()
        self._imgsz = _detection_imgsz()
        self._max_objects = _max_objects()
        self._allowlist = _class_allowlist()

        self._model: Any | None = None
        self._model_lock = threading.Lock()
        self._infer_lock = threading.Lock()
        self._load_failed_at = 0.0
        self._device = "cpu"
        self._names: dict[int, str] = {}
        self._class_filter: list[int] | None = None
        self._last_error = ""
        self._last_latency_ms = 0.0
        # device_id -> (timestamp, detections)
        self._cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._cache_lock = threading.Lock()

        if not self._enabled:
            logger.info("Object detection disabled (OBJECT_DETECTION_ENABLED=0)")

    # ------------------------------------------------------------------ state

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def available(self) -> bool:
        return self._enabled and self._model is not None

    def stats(self) -> dict[str, Any]:
        return {
            "enabled": self._enabled,
            "available": self.available,
            "model": self._model_name,
            "device": self._device,
            "interval_s": self._interval_s,
            "confidence": self._confidence,
            "imgsz": self._imgsz,
            "max_objects": self._max_objects,
            "classes": self._allowlist or "all",
            "last_latency_ms": round(self._last_latency_ms, 1),
            "last_error": self._last_error,
        }

    def latest(self, device_id: str = "default") -> list[dict[str, Any]]:
        """Most recent detections for a device without running inference."""
        with self._cache_lock:
            cached = self._cache.get(device_id)
        return list(cached[1]) if cached else []

    # -------------------------------------------------------------- inference

    def warmup(self) -> None:
        """Load weights and run one dummy pass so the first real frame is fast."""
        if not self._enabled or not self._ensure_model():
            return
        try:
            blank = np.zeros((self._imgsz, self._imgsz, 3), dtype=np.uint8)
            with self._infer_lock:
                self._model.predict(  # type: ignore[union-attr]
                    blank, imgsz=self._imgsz, device=self._device, verbose=False
                )
            logger.info("YOLO26 warmup complete on %s", self._device)
        except Exception as exc:
            logger.warning("YOLO26 warmup failed: %s", exc)

    def detect(
        self,
        frame_bgr: np.ndarray,
        *,
        device_id: str = "default",
        force: bool = False,
    ) -> list[dict[str, Any]]:
        """Detections for a frame, reusing the cached pass inside the interval."""
        if not self._enabled or frame_bgr is None:
            return []

        now = time.time()
        if not force:
            with self._cache_lock:
                cached = self._cache.get(device_id)
                if cached is not None and (now - cached[0]) < self._interval_s:
                    return list(cached[1])

        detections = self._infer(frame_bgr)
        with self._cache_lock:
            self._cache[device_id] = (now, detections)
        return list(detections)

    def _infer(self, frame_bgr: np.ndarray) -> list[dict[str, Any]]:
        if not self._ensure_model():
            return []

        started = time.time()
        try:
            with self._infer_lock:
                results = self._model.predict(  # type: ignore[union-attr]
                    frame_bgr,
                    imgsz=self._imgsz,
                    conf=self._confidence,
                    device=self._device,
                    classes=self._class_filter,
                    verbose=False,
                )
        except Exception as exc:
            self._last_error = str(exc)
            logger.warning("YOLO26 inference failed: %s", exc)
            return []

        self._last_latency_ms = (time.time() - started) * 1000.0
        self._last_error = ""
        return self._parse_results(results, frame_bgr.shape[:2])

    def _parse_results(
        self, results: Any, frame_shape: tuple[int, int]
    ) -> list[dict[str, Any]]:
        if not results:
            return []

        boxes = getattr(results[0], "boxes", None)
        if boxes is None or len(boxes) == 0:
            return []

        frame_h, frame_w = frame_shape
        xyxy = boxes.xyxy.cpu().numpy()
        confs = boxes.conf.cpu().numpy()
        class_ids = boxes.cls.cpu().numpy().astype(int)

        detections: list[dict[str, Any]] = []
        for (x1, y1, x2, y2), conf, class_id in zip(xyxy, confs, class_ids):
            x = max(0, min(int(round(float(x1))), frame_w - 1))
            y = max(0, min(int(round(float(y1))), frame_h - 1))
            w = max(1, min(int(round(float(x2))) - x, frame_w - x))
            h = max(1, min(int(round(float(y2))) - y, frame_h - y))
            detections.append(
                {
                    "label": self._names.get(int(class_id), str(class_id)),
                    "class_id": int(class_id),
                    "confidence": round(float(conf), 3),
                    "box": {"x": x, "y": y, "w": w, "h": h},
                }
            )

        detections.sort(key=lambda det: det["confidence"], reverse=True)
        return detections[: self._max_objects]

    # ------------------------------------------------------------- annotation

    def annotate(
        self, frame: np.ndarray, detections: list[dict[str, Any]]
    ) -> np.ndarray:
        """Draw detection boxes in place.

        Labels sit inside the top-left of each box because FaceService draws its
        name tags just above the face box.
        """
        if not detections:
            return frame

        font = cv2.FONT_HERSHEY_SIMPLEX
        frame_h, frame_w = frame.shape[:2]
        for det in detections:
            box = det.get("box") or {}
            x, y = int(box.get("x", 0)), int(box.get("y", 0))
            w, h = int(box.get("w", 0)), int(box.get("h", 0))
            if w <= 0 or h <= 0:
                continue

            color = _class_color(int(det.get("class_id", 0)))
            cv2.rectangle(frame, (x, y), (x + w, y + h), color, 2, cv2.LINE_AA)

            label = f"{det.get('label', '')} {float(det.get('confidence', 0.0)):.2f}"
            scale = 0.4
            thickness = 1
            (text_w, text_h), baseline = cv2.getTextSize(label, font, scale, thickness)
            tag_w, tag_h = text_w + 8, text_h + baseline + 6
            tag_x = max(0, min(x, frame_w - tag_w))
            tag_y = max(0, min(y, frame_h - tag_h))

            cv2.rectangle(
                frame, (tag_x, tag_y), (tag_x + tag_w, tag_y + tag_h), color, -1
            )
            cv2.putText(
                frame,
                label,
                (tag_x + 4, tag_y + tag_h - baseline - 2),
                font,
                scale,
                (20, 20, 20),
                thickness,
                cv2.LINE_AA,
            )
        return frame

    # ------------------------------------------------------------ model setup

    def _ensure_model(self) -> bool:
        if self._model is not None:
            return True
        if not self._enabled:
            return False
        if self._load_failed_at and (time.time() - self._load_failed_at) < _LOAD_RETRY_SECONDS:
            return False

        with self._model_lock:
            if self._model is not None:
                return True
            try:
                self._load_model()
            except Exception as exc:
                self._load_failed_at = time.time()
                self._last_error = str(exc)
                logger.warning("YOLO26 model unavailable (%s); object detection off", exc)
                return False
        return self._model is not None

    def _load_model(self) -> None:
        from ultralytics import YOLO

        self._device = self._resolve_device()
        model = YOLO(self._resolve_weights_path())
        self._names = {int(k): str(v) for k, v in model.names.items()}
        self._class_filter = self._resolve_class_filter()
        self._model = model
        self._load_failed_at = 0.0
        logger.info(
            "YOLO26 object detection ready (model=%s, device=%s, %d classes)",
            self._model_name,
            self._device,
            len(self._names),
        )

    def _resolve_weights_path(self) -> str:
        """Cache weights alongside the face models instead of the CWD."""
        name = self._model_name
        if os.path.sep in name or name.endswith((".onnx", ".engine")):
            return name

        target = _weights_dir() / name
        if target.is_file():
            return str(target)

        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._download_weights(name, target)
        except Exception as exc:
            logger.info("Could not pre-download %s to %s: %s", name, target.parent, exc)
        return str(target) if target.is_file() else name

    @staticmethod
    def _download_weights(name: str, target: Path) -> None:
        """Fetch release weights into ``target`` via an apostrophe-free staging dir."""
        from ultralytics.utils.downloads import attempt_download_asset

        with tempfile.TemporaryDirectory(prefix="nino-yolo-") as staging:
            downloaded = Path(attempt_download_asset(str(Path(staging) / name)))
            if not downloaded.is_file():
                raise FileNotFoundError(f"{name} was not downloaded")
            # A cache hit resolves outside the staging dir; leave that copy alone.
            if downloaded.parent == Path(staging):
                shutil.move(str(downloaded), str(target))
            else:
                shutil.copy2(str(downloaded), str(target))

    def _resolve_device(self) -> str:
        configured = os.environ.get("OBJECT_DETECTION_DEVICE", "auto").strip().lower()
        if configured and configured != "auto":
            return configured
        try:
            import torch

            if torch.cuda.is_available():
                return "cuda:0"
        except Exception:
            pass
        return "cpu"

    def _resolve_class_filter(self) -> list[int] | None:
        if not self._allowlist:
            return None
        wanted = set(self._allowlist)
        ids = [cid for cid, name in self._names.items() if name.lower() in wanted]
        unknown = wanted - {self._names[cid].lower() for cid in ids}
        if unknown:
            logger.warning("Unknown OBJECT_DETECTION_CLASSES ignored: %s", sorted(unknown))
        return ids or None
