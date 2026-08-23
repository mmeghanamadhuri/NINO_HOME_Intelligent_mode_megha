"""Qwen2.5-VL scene understanding via Ollama — spatial object + scene context."""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import threading
import time
from typing import Any

import cv2
import numpy as np

from vision_pause import vision_inference_paused

logger = logging.getLogger(__name__)

_VISION_PROMPT = (
    "You are the eyes of a home robot camera. Study the image carefully.\n"
    "List visible people and objects with where they are in the frame.\n"
    'Reply ONLY with JSON:\n'
    '{"scene_summary":"one sentence of what the room/scene looks like",'
    '"objects":[{"label":"cup","confidence":0.9,'
    '"position":"left|center|right|top|bottom",'
    '"depth":"near|mid|far"}]}\n'
    "Use lowercase common nouns. Max 12 objects. position and depth are required. "
    "No markdown or explanation."
)

_JSON_OBJECT_RE = re.compile(r"\{[\s\S]*\}")
_LABEL_RE = re.compile(r'"label"\s*:\s*"([^"]+)"')
_CONF_RE = re.compile(r'"confidence"\s*:\s*([0-9.]+)')
_POSITION_RE = re.compile(r'"position"\s*:\s*"([^"]+)"')
_DEPTH_RE = re.compile(r'"depth"\s*:\s*"([^"]+)"')
_SCENE_SUMMARY_RE = re.compile(r'"scene_summary"\s*:\s*"([^"]+)"')


def _vision_model() -> str:
    raw = os.environ.get("OLLAMA_VISION_MODEL", "").strip()
    if raw:
        return raw
    return (
        os.environ.get("OLLAMA_MODEL", "qwen2.5vl:3b").strip() or "qwen2.5vl:3b"
    )


def _vision_interval_s() -> float:
    return max(0.5, float(os.environ.get("VISION_LLM_INTERVAL_S", "1.5")))


def _vision_imgsz() -> int:
    return max(256, int(os.environ.get("VISION_LLM_IMGSZ", "640")))


def _vision_timeout_s() -> int:
    return max(15, int(os.environ.get("VISION_LLM_TIMEOUT_S", "30")))


def _max_objects() -> int:
    return max(1, int(os.environ.get("OBJECT_DETECTION_MAX_OBJECTS", "12")))


def _frame_to_b64_jpeg(frame_bgr: np.ndarray, max_side: int) -> str:
    h, w = frame_bgr.shape[:2]
    if h <= 0 or w <= 0:
        raise RuntimeError("Empty camera frame.")
    scale = min(1.0, max_side / float(max(h, w)))
    img = frame_bgr
    if scale < 1.0:
        img = cv2.resize(
            frame_bgr,
            (max(1, int(w * scale)), max(1, int(h * scale))),
            interpolation=cv2.INTER_AREA,
        )
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 78])
    if not ok:
        raise RuntimeError("JPEG encode failed.")
    return base64.b64encode(buf.tobytes()).decode("ascii")


def _repair_json_blob(blob: str) -> dict[str, Any] | None:
    for suffix in ("", "]", "}]}", "}"):
        try:
            payload = json.loads(blob + suffix)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _normalize_position(raw: Any) -> str:
    pos = str(raw or "").strip().lower()
    if pos in {"left", "center", "centre", "middle", "right", "top", "bottom"}:
        return "centre" if pos in {"center", "middle"} else pos
    for token in pos.replace("-", " ").split():
        if token in {"left", "right", "top", "bottom", "centre", "center"}:
            return "centre" if token == "center" else token
    return ""


def _normalize_depth(raw: Any) -> str:
    depth = str(raw or "").strip().lower()
    if depth in {"near", "close", "foreground", "front"}:
        return "near"
    if depth in {"far", "background", "back", "distant"}:
        return "far"
    if depth in {"mid", "middle", "medium"}:
        return "mid"
    return ""


def _parse_scene(text: str) -> tuple[list[dict[str, Any]], str]:
    raw = str(text or "").strip()
    if not raw:
        return [], ""
    scene_summary = ""
    match = _JSON_OBJECT_RE.search(raw)
    items: list[Any] = []
    if match:
        blob = match.group(0)
        try:
            payload = json.loads(blob)
        except json.JSONDecodeError:
            payload = _repair_json_blob(blob)
        if isinstance(payload, dict):
            scene_summary = str(payload.get("scene_summary") or "").strip()
            maybe = payload.get("objects")
            if isinstance(maybe, list):
                items = maybe
        elif isinstance(payload, list):
            items = payload
    if not scene_summary:
        sm = _SCENE_SUMMARY_RE.search(raw)
        if sm:
            scene_summary = sm.group(1).strip()
    if not items:
        labels = _LABEL_RE.findall(raw)
        confs = [float(c) for c in _CONF_RE.findall(raw)]
        positions = _POSITION_RE.findall(raw)
        depths = _DEPTH_RE.findall(raw)
        if labels:
            items = [
                {
                    "label": label,
                    "confidence": confs[i] if i < len(confs) else 0.75,
                    "position": positions[i] if i < len(positions) else "",
                    "depth": depths[i] if i < len(depths) else "",
                }
                for i, label in enumerate(labels)
            ]
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip().lower()
        if not label or label in {"unknown", "none"}:
            continue
        try:
            conf = float(item.get("confidence", 0.75))
        except (TypeError, ValueError):
            conf = 0.75
        position = _normalize_position(item.get("position"))
        depth = _normalize_depth(item.get("depth"))
        out.append(
            {
                "label": label,
                "class_id": -1,
                "confidence": round(min(1.0, max(0.05, conf)), 3),
                "box": None,
                "position": position,
                "depth": depth,
            }
        )
    return out[: _max_objects()], scene_summary


class VisionSceneService:
    """Throttled Qwen-VL passes over camera frames with spatial scene memory."""

    def __init__(self) -> None:
        self._interval_s = _vision_interval_s()
        self._imgsz = _vision_imgsz()
        self._model = _vision_model()
        self._infer_lock = threading.Lock()
        self._cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._summary_cache: dict[str, tuple[float, str]] = {}
        self._cache_lock = threading.Lock()
        self._last_error = ""
        self._last_latency_ms = 0.0
        self._generation = 0

    def bump_generation(self) -> None:
        """Invalidate in-flight background inference (spatial scan starting/ending)."""
        with self._infer_lock:
            self._generation += 1

    def stats(self) -> dict[str, Any]:
        return {
            "backend": "qwen-vl",
            "model": self._model,
            "interval_s": self._interval_s,
            "imgsz": self._imgsz,
            "last_latency_ms": round(self._last_latency_ms, 1),
            "last_error": self._last_error,
        }

    def scene_summary(self, device_id: str = "default") -> str:
        with self._cache_lock:
            cached = self._summary_cache.get(device_id)
        return cached[1] if cached else ""

    def clear_device(self, device_id: str = "default") -> None:
        key = str(device_id or "").strip() or "default"
        with self._cache_lock:
            self._cache.pop(key, None)
            self._summary_cache.pop(key, None)

    def warmup(self) -> None:
        try:
            blank = np.zeros((self._imgsz, self._imgsz, 3), dtype=np.uint8)
            self._infer(blank)
            logger.info("Qwen-VL vision warmup complete model=%s", self._model)
        except Exception as exc:
            logger.warning("Qwen-VL vision warmup failed: %s", exc)

    def detect(
        self,
        frame_bgr: np.ndarray,
        *,
        device_id: str = "default",
        force: bool = False,
    ) -> list[dict[str, Any]]:
        if frame_bgr is None:
            return []
        now = time.time()
        if not force and vision_inference_paused(device_id):
            with self._cache_lock:
                cached = self._cache.get(device_id)
            return list(cached[1]) if cached else []
        if not force:
            with self._cache_lock:
                cached = self._cache.get(device_id)
                if cached is not None and (now - cached[0]) < self._interval_s:
                    return list(cached[1])
        with self._infer_lock:
            generation = self._generation
        detections, summary = self._infer(frame_bgr)
        if not force and (
            vision_inference_paused(device_id) or generation != self._generation
        ):
            with self._cache_lock:
                cached = self._cache.get(device_id)
            return list(cached[1]) if cached else []
        with self._cache_lock:
            self._cache[device_id] = (now, detections)
            if summary:
                self._summary_cache[device_id] = (now, summary)
        return list(detections)

    def _infer(self, frame_bgr: np.ndarray) -> tuple[list[dict[str, Any]], str]:
        from llm_service import ollama_chat

        started = time.time()
        try:
            image_b64 = _frame_to_b64_jpeg(frame_bgr, self._imgsz)
            with self._infer_lock:
                text = ollama_chat(
                    [
                        {
                            "role": "user",
                            "content": _VISION_PROMPT,
                            "images": [image_b64],
                        }
                    ],
                    model=self._model,
                    timeout_s=_vision_timeout_s(),
                    num_predict=256,
                    temperature=0.1,
                )
            detections, scene_summary = _parse_scene(text)
            self._last_latency_ms = (time.time() - started) * 1000.0
            self._last_error = ""
            logger.debug(
                "Vision LLM %s objects in %.0fms summary=%s labels=%s",
                len(detections),
                self._last_latency_ms,
                (scene_summary[:80] + "…") if len(scene_summary) > 80 else scene_summary,
                [d["label"] for d in detections],
            )
            return detections, scene_summary
        except Exception as exc:
            self._last_error = str(exc)
            self._last_latency_ms = (time.time() - started) * 1000.0
            logger.warning("Vision LLM inference failed: %s", exc)
            return [], ""
