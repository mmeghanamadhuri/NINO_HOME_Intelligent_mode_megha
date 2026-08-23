"""Open-palm stop gesture: raise a hand toward the camera to interrupt Nino."""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

_TIP_PIP = ((8, 6), (12, 10), (16, 14), (20, 18))
_THUMB = (4, 3)


def _env_flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _dist(a: Any, b: Any) -> float:
    return float(((a.x - b.x) ** 2 + (a.y - b.y) ** 2) ** 0.5)


def hand_bbox_frac(landmarks: Any) -> float:
    xs = [float(p.x) for p in landmarks]
    ys = [float(p.y) for p in landmarks]
    return max(0.0, max(xs) - min(xs)) * max(0.0, max(ys) - min(ys))


def is_open_palm_landmarks(
    landmarks: Any,
    *,
    min_extended: int = 4,
    min_area: float = 0.03,
) -> bool:
    """True when four-plus fingers are stretched and the hand is close/in front."""
    if landmarks is None or len(landmarks) < 21:
        return False
    if hand_bbox_frac(landmarks) < min_area:
        return False
    wrist = landmarks[0]
    extended = 0
    for tip_i, pip_i in _TIP_PIP:
        if _dist(landmarks[tip_i], wrist) > _dist(landmarks[pip_i], wrist) * 1.12:
            extended += 1
    tip_i, ip_i = _THUMB
    if _dist(landmarks[tip_i], wrist) > _dist(landmarks[ip_i], wrist) * 1.08:
        extended += 1
    return extended >= min_extended


def is_raised_wrist_pose(keypoints: list, *, conf_min: float = 0.35) -> bool:
    """YOLO-pose fallback: a wrist is up and toward the camera."""
    if not keypoints or len(keypoints) < 11:
        return False

    def _pt(idx: int) -> tuple[float, float, float] | None:
        if idx >= len(keypoints):
            return None
        item = keypoints[idx]
        if item is None or len(item) < 2:
            return None
        x, y = float(item[0]), float(item[1])
        c = float(item[2]) if len(item) > 2 else 1.0
        if c < conf_min or x < 0 or y < 0:
            return None
        return x, y, c

    for shoulder_i, elbow_i, wrist_i in ((5, 7, 9), (6, 8, 10)):
        shoulder = _pt(shoulder_i)
        elbow = _pt(elbow_i)
        wrist = _pt(wrist_i)
        if not (shoulder and elbow and wrist):
            continue
        if wrist[1] < elbow[1] <= shoulder[1] + 0.08 and 0.15 <= wrist[0] <= 0.85:
            if wrist[1] <= 0.58:
                return True
    return False


class PalmGestureService:
    """Debounced open-palm detector used by the background vision loop."""

    def __init__(self) -> None:
        self.enabled = _env_flag("PALM_GESTURE_ENABLED", "1")
        self.min_area = _env_float("PALM_MIN_AREA", 0.03)
        self.hits_needed = max(1, int(_env_float("PALM_HITS_NEEDED", 2)))
        self.cooldown_s = _env_float("PALM_COOLDOWN_S", 2.5)
        self.interval_s = _env_float("PALM_DETECT_INTERVAL_S", 0.18)
        self._lock = threading.Lock()
        self._hands: Any | None = None
        self._pose: Any | None = None
        self._backend = ""
        self._hits: dict[str, int] = {}
        self._last_try: dict[str, float] = {}
        self._last_fire: dict[str, float] = {}

    def _ensure_backend(self) -> str:
        if self._backend:
            return self._backend
        try:
            import mediapipe as mp

            self._hands = mp.solutions.hands.Hands(
                static_image_mode=False,
                max_num_hands=2,
                model_complexity=0,
                min_detection_confidence=0.55,
                min_tracking_confidence=0.5,
            )
            self._backend = "mediapipe"
            logger.info("Palm gesture: MediaPipe Hands")
            return self._backend
        except Exception:
            logger.info("Palm gesture: MediaPipe unavailable, trying YOLO pose")
        try:
            from ultralytics import YOLO

            self._pose = YOLO(os.environ.get("PALM_POSE_MODEL", "yolo11n-pose.pt"))
            self._backend = "yolo_pose"
            logger.info("Palm gesture: YOLO pose fallback")
            return self._backend
        except Exception:
            logger.warning("Palm gesture disabled — no MediaPipe or YOLO pose")
            self._backend = "none"
            return self._backend

    def detect_open_palm(self, frame) -> bool:
        if frame is None:
            return False
        backend = self._ensure_backend()
        if backend == "mediapipe":
            return self._detect_mediapipe(frame)
        if backend == "yolo_pose":
            return self._detect_pose(frame)
        return False

    def _detect_mediapipe(self, frame) -> bool:
        import cv2

        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        result = self._hands.process(rgb)
        if not result.multi_hand_landmarks:
            return False
        for hand in result.multi_hand_landmarks:
            if is_open_palm_landmarks(hand.landmark, min_area=self.min_area):
                return True
        return False

    def _detect_pose(self, frame) -> bool:
        results = self._pose.predict(frame, verbose=False, imgsz=320)
        if not results:
            return False
        kps = getattr(results[0], "keypoints", None)
        if kps is None or getattr(kps, "xyn", None) is None:
            return False
        data = kps.xyn.cpu().numpy() if hasattr(kps.xyn, "cpu") else np.array(kps.xyn)
        conf = None
        if getattr(kps, "conf", None) is not None:
            conf = kps.conf.cpu().numpy() if hasattr(kps.conf, "cpu") else np.array(kps.conf)
        for i, person in enumerate(data):
            pts = []
            for j, xy in enumerate(person):
                c = float(conf[i][j]) if conf is not None and i < len(conf) else 1.0
                pts.append((float(xy[0]), float(xy[1]), c))
            if is_raised_wrist_pose(pts):
                return True
        return False

    def maybe_trigger(self, frame, device_id: str | None) -> bool:
        if not self.enabled or frame is None:
            return False
        key = str(device_id or "").strip() or "-"
        now = time.time()
        with self._lock:
            if now - self._last_try.get(key, 0.0) < self.interval_s:
                return False
            self._last_try[key] = now
            if now - self._last_fire.get(key, 0.0) < self.cooldown_s:
                return False
        found = False
        try:
            found = self.detect_open_palm(frame)
        except Exception:
            logger.debug("Palm detect failed device=%s", key, exc_info=True)
            found = False
        with self._lock:
            if not found:
                self._hits[key] = 0
                return False
            hits = self._hits.get(key, 0) + 1
            self._hits[key] = hits
            if hits < self.hits_needed:
                return False
            self._hits[key] = 0
            self._last_fire[key] = now
        logger.info("Palm stop gesture device=%s backend=%s", key, self._backend)
        return True


_service: PalmGestureService | None = None
_service_lock = threading.Lock()


def get_palm_gesture_service() -> PalmGestureService:
    global _service
    with _service_lock:
        if _service is None:
            _service = PalmGestureService()
        return _service


def request_esp_palm_listen(device_id: str | None = None) -> tuple[bool, str | None]:
    """POST /gesture/listen on the ESP. Returns (ok, error_code)."""
    import requests
    from esp_playback import device_base_url

    base = device_base_url(device_id)
    if not base:
        return False, "no_esp_url"
    url = f"{base.rstrip('/')}/gesture/listen"
    try:
        resp = requests.post(url, timeout=4)
        if resp.status_code == 200:
            return True, None
        return False, f"http_{resp.status_code}"
    except requests.RequestException as exc:
        logger.warning("ESP palm listen request failed: %s", exc)
        return False, "request_failed"
