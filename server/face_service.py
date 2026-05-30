from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import cv2
import numpy as np

logger = logging.getLogger(__name__)

FACE_SIZE = (200, 200)
YUNET_FILENAME = "face_detection_yunet_2023mar.onnx"
YUNET_MODEL_URL = (
    "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/"
    + YUNET_FILENAME
)


class FaceService:
    def __init__(self, data_dir: Path, recognition_threshold: float | None = None) -> None:
        self.data_dir = data_dir
        self.faces_dir = data_dir / "faces"
        self.model_path = data_dir / "face_model.yml"
        self.labels_path = data_dir / "labels.json"
        self._yunet_model_path = data_dir / "models" / YUNET_FILENAME
        self._lock = threading.Lock()
        self._labels: dict[int, str] = {}
        self._confirm_tracks: list[dict[str, Any]] = []
        self._recognizer = self._create_recognizer()
        self._yunet: Any | None = None
        self._yunet_enabled = False
        if recognition_threshold is not None:
            os.environ["FACE_RECOGNITION_THRESHOLD"] = str(recognition_threshold)
        self.apply_settings_from_environ()
        self._cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        )
        self._clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))

        self.faces_dir.mkdir(parents=True, exist_ok=True)
        self._load_model()
        self._init_yunet_detector()

    def apply_settings_from_environ(self) -> None:
        """Reload tunables from environment (also used at startup / CLI)."""
        self.recognition_threshold = float(os.environ.get("FACE_RECOGNITION_THRESHOLD", "80"))
        self.soft_recognition_threshold = float(
            os.environ.get("FACE_SOFT_RECOGNITION_THRESHOLD", "92")
        )
        grace_delta = float(os.environ.get("FACE_UNKNOWN_GRACE_DELTA", "16"))
        self.unknown_grace_threshold = self.recognition_threshold + grace_delta
        self.track_hold_seconds = float(os.environ.get("FACE_TRACK_HOLD_SECONDS", "2.0"))
        self.track_match_iou = float(os.environ.get("FACE_TRACK_MATCH_IOU", "0.20"))
        self.confirm_frames = max(1, int(os.environ.get("FACE_CONFIRM_FRAMES", "2")))
        self.weak_confirm_frames = max(
            self.confirm_frames, int(os.environ.get("FACE_WEAK_CONFIRM_FRAMES", "3"))
        )
        self.detect_min_neighbors = int(os.environ.get("FACE_DETECT_MIN_NEIGHBORS", "4"))
        self.detect_fallback_neighbors = int(os.environ.get("FACE_DETECT_FALLBACK_NEIGHBORS", "3"))
        self.detect_min_size = int(os.environ.get("FACE_DETECT_MIN_SIZE", "32"))
        self.yunet_score_threshold = float(os.environ.get("FACE_YUNET_SCORE", "0.45"))
        self.face_pad_ratio = float(os.environ.get("FACE_CROP_PAD_RATIO", "0.24"))
        self.min_face_area_ratio = float(os.environ.get("FACE_MIN_AREA_RATIO", "0.0025"))
        self.min_face_aspect = float(os.environ.get("FACE_MIN_ASPECT", "0.68"))
        self.max_face_aspect = float(os.environ.get("FACE_MAX_ASPECT", "1.42"))

    @property
    def recognizer_available(self) -> bool:
        return self._recognizer is not None

    def stats(self) -> dict[str, Any]:
        people = []
        for person_dir in sorted(self.faces_dir.iterdir()):
            if person_dir.is_dir():
                people.append(
                    {
                        "id": person_dir.name,
                        "name": self._display_name(person_dir.name),
                        "samples": len(list(person_dir.glob("*.jpg"))),
                    }
                )

        return {
            "recognizer_available": self.recognizer_available,
            "people": people,
            "trained_people": len(self._labels),
            "threshold": self.recognition_threshold,
            "soft_threshold": self.soft_recognition_threshold,
            "unknown_grace_threshold": self.unknown_grace_threshold,
            "confirm_frames": self.confirm_frames,
            "weak_confirm_frames": self.weak_confirm_frames,
            "track_hold_seconds": self.track_hold_seconds,
            "detect_min_neighbors": self.detect_min_neighbors,
            "detector": "yunet" if self._yunet_enabled else "haar",
        }

    def detect(self, frame: np.ndarray) -> list[tuple[int, int, int, int]]:
        if frame is None or frame.size == 0:
            return []

        h, w = frame.shape[:2]
        if h < 32 or w < 32:
            return []

        boxes: list[tuple[int, int, int, int]] = []
        if self._yunet_enabled and self._yunet is not None:
            boxes = self._detect_yunet(frame)

        if not boxes:
            boxes = self._detect_haar(frame)

        gray_shape = (h, w) if frame.ndim == 2 else frame.shape[:2]
        return self._filter_face_boxes(boxes, gray_shape)

    def _detect_yunet(self, frame: np.ndarray) -> list[tuple[int, int, int, int]]:
        assert self._yunet is not None
        h, w = frame.shape[:2]
        bgr = frame if frame.ndim == 3 else cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
        try:
            self._yunet.setInputSize((w, h))
            _, faces = self._yunet.detect(bgr)
        except cv2.error:
            return []

        if faces is None or len(faces) == 0:
            return []

        boxes: list[tuple[int, int, int, int]] = []
        for row in faces:
            if len(row) < 15:
                continue
            score = float(row[14])
            if score < self.yunet_score_threshold:
                continue
            x, y, bw, bh = int(row[0]), int(row[1]), int(row[2]), int(row[3])
            if bw <= 0 or bh <= 0:
                continue
            boxes.append((x, y, bw, bh))
        return boxes

    def _detect_haar(self, frame: np.ndarray) -> list[tuple[int, int, int, int]]:
        gray = self._frame_gray(frame)
        h, w = gray.shape[:2]

        detect_gray = gray
        scale_back = 1.0
        max_side = max(h, w)
        if max_side > 960:
            scale_back = 960.0 / max_side
            detect_gray = cv2.resize(
                gray,
                (max(32, int(w * scale_back)), max(32, int(h * scale_back))),
                interpolation=cv2.INTER_AREA,
            )

        faces = self._detect_multiscale_safe(
            detect_gray,
            scale_factor=1.08,
            min_neighbors=self.detect_min_neighbors,
            min_size=self.detect_min_size,
        )
        if len(faces) == 0:
            faces = self._detect_multiscale_safe(
                detect_gray,
                scale_factor=1.08,
                min_neighbors=self.detect_fallback_neighbors,
                min_size=max(28, self.detect_min_size - 8),
            )

        if scale_back != 1.0 and len(faces) > 0:
            inv = 1.0 / scale_back
            faces = np.round(faces.astype(np.float64) * inv).astype(np.int32)

        return [(int(x), int(y), int(w), int(h)) for x, y, w, h in faces]

    def _detect_multiscale_safe(
        self,
        gray: np.ndarray,
        *,
        scale_factor: float,
        min_neighbors: int,
        min_size: int,
    ) -> np.ndarray:
        h, w = gray.shape[:2]
        if h < 32 or w < 32:
            return np.empty((0, 4), dtype=np.int32)

        cap = min(h, w) - 8
        ms = min(min_size, cap)
        if ms < 24:
            return np.empty((0, 4), dtype=np.int32)

        try:
            found = self._cascade.detectMultiScale(
                gray,
                scaleFactor=scale_factor,
                minNeighbors=min_neighbors,
                minSize=(ms, ms),
                flags=cv2.CASCADE_SCALE_IMAGE,
            )
        except cv2.error:
            return np.empty((0, 4), dtype=np.int32)

        if found is None or len(found) == 0:
            return np.empty((0, 4), dtype=np.int32)
        return np.asarray(found, dtype=np.int32)

    def register_sample(self, name: str, frame: np.ndarray) -> Path:
        person_id = self._person_id(name)
        if not person_id:
            raise ValueError("Person name is required")

        if frame is None or frame.size == 0:
            raise ValueError("Invalid camera frame")
        fh, fw = frame.shape[:2]
        if fh < 60 or fw < 60:
            raise ValueError("Camera frame too small — wait for the stream to connect")

        faces = self.detect(frame)
        if not faces:
            raise ValueError("No face detected in frame")

        x, y, w, h = max(faces, key=lambda box: box[2] * box[3])
        face = self._normalize_face(frame, (x, y, w, h))

        person_dir = self.faces_dir / person_id
        person_dir.mkdir(parents=True, exist_ok=True)
        output_path = person_dir / f"{int(time.time() * 1000)}.jpg"
        cv2.imwrite(str(output_path), face)
        return output_path

    def train(self) -> dict[str, Any]:
        if self._recognizer is None:
            raise RuntimeError(
                "OpenCV face recognizer is unavailable. Install opencv-contrib-python."
            )

        images: list[np.ndarray] = []
        label_ids: list[int] = []
        labels: dict[int, str] = {}

        for label_id, person_dir in enumerate(sorted(self.faces_dir.iterdir())):
            if not person_dir.is_dir():
                continue

            sample_paths = sorted(person_dir.glob("*.jpg"))
            if not sample_paths:
                continue

            labels[label_id] = self._display_name(person_dir.name)
            for sample_path in sample_paths:
                image = cv2.imread(str(sample_path), cv2.IMREAD_GRAYSCALE)
                if image is None:
                    continue
                face = cv2.resize(image, FACE_SIZE)
                for variant in self._augment_training_face(face):
                    images.append(self._enhance_gray(variant))
                    label_ids.append(label_id)

        if not images:
            raise ValueError("No registered face samples found")

        recognizer = self._create_recognizer()
        if recognizer is None:
            raise RuntimeError("OpenCV face recognizer is unavailable")

        recognizer.train(images, np.array(label_ids))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        recognizer.write(str(self.model_path))
        self.labels_path.write_text(json.dumps(labels, indent=2), encoding="utf-8")

        with self._lock:
            self._recognizer = recognizer
            self._labels = labels

        return {"people": len(labels), "samples": len(images)}

    def recognize(self, frame: np.ndarray) -> list[dict[str, Any]]:
        faces = self.detect(frame)
        results: list[dict[str, Any]] = []
        now = time.time()
        self._confirm_tracks = [
            t
            for t in self._confirm_tracks
            if (now - float(t["seen_at"])) <= self.track_hold_seconds
        ]

        with self._lock:
            recognizer = self._recognizer
            labels = dict(self._labels)

        for x, y, w, h in faces:
            name = "Face"
            confidence: float | None = None
            recognized = False
            used_track = False
            pending = False
            strict_name: str | None = None
            weak_name: str | None = None

            if recognizer is not None and labels:
                face = self._normalize_face(frame, (x, y, w, h))
                label_id, raw_confidence = recognizer.predict(face)
                confidence = float(raw_confidence)
                best_name = labels.get(int(label_id), "Unknown")
                if confidence <= self.recognition_threshold:
                    strict_name = best_name
                elif confidence <= self.soft_recognition_threshold:
                    weak_name = best_name

                name, recognized, used_track, pending = self._apply_confirmation(
                    (x, y, w, h), strict_name, weak_name, confidence, now
                )

            results.append(
                {
                    "box": {"x": x, "y": y, "w": w, "h": h},
                    "name": name,
                    "recognized": recognized,
                    "confidence": confidence,
                    "stabilized": used_track,
                    "pending": pending,
                }
            )

        return results

    def annotate(self, frame: np.ndarray) -> tuple[np.ndarray, list[dict[str, Any]]]:
        output = frame.copy()
        results = self.recognize(frame)

        for result in results:
            box = result["box"]
            x, y, w, h = box["x"], box["y"], box["w"], box["h"]
            if result["recognized"]:
                color = (0, 200, 0)
            elif result.get("pending"):
                color = (0, 220, 220)
            else:
                color = (0, 180, 255)
            label = result["name"]
            if result["confidence"] is not None:
                label = f"{label} ({result['confidence']:.0f})"

            cv2.rectangle(output, (x, y), (x + w, y + h), color, 2)
            cv2.putText(
                output,
                label,
                (x, max(24, y - 10)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7,
                color,
                2,
                cv2.LINE_AA,
            )

        return output, results

    def _filter_face_boxes(
        self, boxes: list[tuple[int, int, int, int]], frame_shape: tuple[int, ...]
    ) -> list[tuple[int, int, int, int]]:
        fh, fw = frame_shape[:2]
        frame_area = max(1, fh * fw)
        kept: list[tuple[int, int, int, int]] = []
        for x, y, w, h in boxes:
            if w <= 0 or h <= 0:
                continue
            if (w * h) / frame_area < self.min_face_area_ratio:
                continue
            aspect = w / float(h)
            if aspect < self.min_face_aspect or aspect > self.max_face_aspect:
                continue
            kept.append((x, y, w, h))
        return kept

    def _match_confirm_track(
        self, box: tuple[int, int, int, int], now: float
    ) -> dict[str, Any] | None:
        best: dict[str, Any] | None = None
        best_iou = 0.0
        for track in self._confirm_tracks:
            if (now - float(track["seen_at"])) > self.track_hold_seconds:
                continue
            iou = self._iou(box, tuple(track["box"]))
            if iou > self.track_match_iou and iou > best_iou:
                best_iou = iou
                best = track
        return best

    def _apply_confirmation(
        self,
        box: tuple[int, int, int, int],
        strict_name: str | None,
        weak_name: str | None,
        confidence: float | None,
        now: float,
    ) -> tuple[str, bool, bool, bool]:
        """Returns (display_name, recognized, stabilized, pending)."""
        track = self._match_confirm_track(box, now)
        if track is None:
            track = {
                "box": box,
                "seen_at": now,
                "candidate": None,
                "streak": 0,
                "confirmed_name": None,
                "weak_streak": False,
            }
            self._confirm_tracks.append(track)
        else:
            track["box"] = box
            track["seen_at"] = now

        stabilized = False
        pending = False

        active_name = strict_name
        confirm_needed = self.confirm_frames
        is_weak = False
        if not active_name and weak_name:
            active_name = weak_name
            confirm_needed = self.weak_confirm_frames
            is_weak = True

        if active_name and active_name not in {"Unknown", "Face"}:
            if track.get("candidate") == active_name and bool(track.get("weak_streak")) == is_weak:
                track["streak"] = int(track.get("streak", 0)) + 1
            else:
                track["candidate"] = active_name
                track["streak"] = 1
                track["weak_streak"] = is_weak
        elif not track.get("confirmed_name"):
            track["streak"] = 0
            track["candidate"] = None
            track["weak_streak"] = False

        if int(track.get("streak", 0)) >= confirm_needed:
            track["confirmed_name"] = str(track["candidate"])
        elif track.get("confirmed_name") and confidence is not None:
            if confidence <= self.unknown_grace_threshold:
                stabilized = True
            else:
                track["confirmed_name"] = None
                track["streak"] = 0

        confirmed = track.get("confirmed_name")
        if confirmed:
            return str(confirmed), True, stabilized, False

        if active_name and active_name not in {"Unknown", "Face"}:
            pending = int(track.get("streak", 0)) > 0
            return active_name, False, False, pending

        return "Unknown", False, False, False

    def _iou(
        self, a: tuple[int, int, int, int], b: tuple[int, int, int, int]
    ) -> float:
        ax, ay, aw, ah = a
        bx, by, bw, bh = b
        ax2, ay2 = ax + aw, ay + ah
        bx2, by2 = bx + bw, by + bh

        ix1 = max(ax, bx)
        iy1 = max(ay, by)
        ix2 = min(ax2, bx2)
        iy2 = min(ay2, by2)
        iw = max(0, ix2 - ix1)
        ih = max(0, iy2 - iy1)
        inter = iw * ih
        if inter <= 0:
            return 0.0

        area_a = aw * ah
        area_b = bw * bh
        union = area_a + area_b - inter
        if union <= 0:
            return 0.0
        return float(inter) / float(union)

    def _load_model(self) -> None:
        if self._recognizer is None or not self.model_path.exists() or not self.labels_path.exists():
            return

        labels_raw = json.loads(self.labels_path.read_text(encoding="utf-8"))
        self._labels = {int(key): value for key, value in labels_raw.items()}
        self._recognizer.read(str(self.model_path))

    def _init_yunet_detector(self) -> None:
        if not hasattr(cv2, "FaceDetectorYN"):
            logger.warning("YuNet unavailable — using Haar cascade for face detection")
            return
        if not self._ensure_yunet_model():
            logger.warning("YuNet model missing — using Haar cascade for face detection")
            return
        try:
            self._yunet = cv2.FaceDetectorYN.create(
                str(self._yunet_model_path),
                "",
                (320, 320),
                self.yunet_score_threshold,
                0.35,
                5000,
            )
            self._yunet_enabled = True
            logger.info("YuNet face detector ready (better at ~1 m distance)")
        except cv2.error as exc:
            logger.warning("YuNet init failed (%s) — using Haar cascade", exc)

    def _ensure_yunet_model(self) -> bool:
        if self._yunet_model_path.is_file() and self._yunet_model_path.stat().st_size > 100_000:
            return True
        self._yunet_model_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            logger.info("Downloading YuNet model to %s", self._yunet_model_path)
            with urllib.request.urlopen(YUNET_MODEL_URL, timeout=60) as resp:
                data = resp.read()
            self._yunet_model_path.write_bytes(data)
            return True
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            logger.warning("Could not download YuNet model: %s", exc)
            return False

    def _frame_gray(self, frame: np.ndarray) -> np.ndarray:
        if frame.ndim == 2:
            gray = frame
        else:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        return self._enhance_gray(gray)

    def _enhance_gray(self, gray: np.ndarray) -> np.ndarray:
        return self._clahe.apply(gray)

    def _normalize_face(
        self, frame: np.ndarray, box: tuple[int, int, int, int]
    ) -> np.ndarray:
        x, y, w, h = box
        fh, fw = frame.shape[:2]
        pad = int(max(w, h) * self.face_pad_ratio)
        x1 = max(0, x - pad)
        y1 = max(0, y - pad)
        x2 = min(fw, x + w + pad)
        y2 = min(fh, y + h + pad)

        gray = self._frame_gray(frame)
        face = gray[y1:y2, x1:x2]
        if face.size == 0:
            face = gray[y : y + h, x : x + w]

        face = cv2.bilateralFilter(face, d=5, sigmaColor=28, sigmaSpace=28)

        min_side = min(face.shape[:2])
        if min_side < 72:
            scale = 72.0 / max(1, min_side)
            face = cv2.resize(
                face,
                (max(1, int(face.shape[1] * scale)), max(1, int(face.shape[0] * scale))),
                interpolation=cv2.INTER_CUBIC,
            )

        return cv2.resize(face, FACE_SIZE, interpolation=cv2.INTER_CUBIC)

    @staticmethod
    def _augment_training_face(face: np.ndarray) -> list[np.ndarray]:
        """Synthetic views for distance, blur, and lighting (ESP MJPEG stream)."""
        base = cv2.resize(face, FACE_SIZE)
        variants: list[np.ndarray] = []

        def add(img: np.ndarray) -> None:
            variants.append(img)

        for scale in (0.75, 0.86, 1.0, 1.10):
            if abs(scale - 1.0) < 1e-3:
                add(base)
                continue
            h, w = base.shape[:2]
            nh, nw = max(16, int(h * scale)), max(16, int(w * scale))
            scaled = cv2.resize(
                base,
                (nw, nh),
                interpolation=cv2.INTER_CUBIC if scale > 1.0 else cv2.INTER_AREA,
            )
            if scale < 1.0:
                scaled = cv2.resize(scaled, FACE_SIZE, interpolation=cv2.INTER_CUBIC)
            else:
                sh, sw = scaled.shape[:2]
                y0 = max(0, (sh - FACE_SIZE[0]) // 2)
                x0 = max(0, (sw - FACE_SIZE[1]) // 2)
                crop = scaled[y0 : y0 + FACE_SIZE[0], x0 : x0 + FACE_SIZE[1]]
                scaled = crop if crop.shape[:2] == FACE_SIZE else cv2.resize(crop, FACE_SIZE)
            add(scaled)

        out: list[np.ndarray] = []
        for v in variants:
            out.append(v)
            out.append(cv2.GaussianBlur(v, (0, 0), 0.85))
            out.append(cv2.convertScaleAbs(v, alpha=1.10, beta=10))
            out.append(cv2.convertScaleAbs(v, alpha=0.90, beta=-10))
        return out

    def _create_recognizer(self) -> Any | None:
        if not hasattr(cv2, "face"):
            return None
        return cv2.face.LBPHFaceRecognizer_create(radius=2, neighbors=8, grid_x=8, grid_y=8)

    def _person_id(self, name: str) -> str:
        cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "_", name.strip().lower())
        return cleaned.strip("_")

    def _display_name(self, person_id: str) -> str:
        return person_id.replace("_", " ").title()
