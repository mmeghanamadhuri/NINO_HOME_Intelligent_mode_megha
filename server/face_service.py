from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np


FACE_SIZE = (200, 200)


class FaceService:
    def __init__(self, data_dir: Path, recognition_threshold: float | None = None) -> None:
        self.data_dir = data_dir
        self.faces_dir = data_dir / "faces"
        self.model_path = data_dir / "face_model.yml"
        self.labels_path = data_dir / "labels.json"
        self._lock = threading.Lock()
        self._labels: dict[int, str] = {}
        self._confirm_tracks: list[dict[str, Any]] = []
        self._recognizer = self._create_recognizer()
        if recognition_threshold is not None:
            os.environ["FACE_RECOGNITION_THRESHOLD"] = str(recognition_threshold)
        self.apply_settings_from_environ()
        self._cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        )
        self._clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))

        self.faces_dir.mkdir(parents=True, exist_ok=True)
        self._load_model()

    def apply_settings_from_environ(self) -> None:
        """Reload tunables from environment (also used at startup / CLI)."""
        self.recognition_threshold = float(os.environ.get("FACE_RECOGNITION_THRESHOLD", "58"))
        grace_delta = float(os.environ.get("FACE_UNKNOWN_GRACE_DELTA", "8"))
        self.unknown_grace_threshold = self.recognition_threshold + grace_delta
        self.track_hold_seconds = float(os.environ.get("FACE_TRACK_HOLD_SECONDS", "1.4"))
        self.track_match_iou = float(os.environ.get("FACE_TRACK_MATCH_IOU", "0.22"))
        self.confirm_frames = max(1, int(os.environ.get("FACE_CONFIRM_FRAMES", "3")))
        self.detect_min_neighbors = int(os.environ.get("FACE_DETECT_MIN_NEIGHBORS", "5"))
        self.detect_fallback_neighbors = int(os.environ.get("FACE_DETECT_FALLBACK_NEIGHBORS", "4"))
        self.detect_min_size = int(os.environ.get("FACE_DETECT_MIN_SIZE", "56"))
        self.min_face_area_ratio = float(os.environ.get("FACE_MIN_AREA_RATIO", "0.004"))
        self.min_face_aspect = float(os.environ.get("FACE_MIN_ASPECT", "0.72"))
        self.max_face_aspect = float(os.environ.get("FACE_MAX_ASPECT", "1.38"))

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
            "unknown_grace_threshold": self.unknown_grace_threshold,
            "confirm_frames": self.confirm_frames,
            "track_hold_seconds": self.track_hold_seconds,
            "detect_min_neighbors": self.detect_min_neighbors,
        }

    def detect(self, frame: np.ndarray) -> list[tuple[int, int, int, int]]:
        if frame is None or frame.size == 0:
            return []

        gray = self._frame_gray(frame)
        h, w = gray.shape[:2]
        if h < 32 or w < 32:
            return []

        # Downscale for the detector on large ESP frames (faster, fewer OpenCV pyramid bugs).
        detect_gray = gray
        scale_back = 1.0
        max_side = max(h, w)
        if max_side > 720:
            scale_back = 720.0 / max_side
            detect_gray = cv2.resize(
                gray,
                (max(32, int(w * scale_back)), max(32, int(h * scale_back))),
                interpolation=cv2.INTER_AREA,
            )

        faces = self._detect_multiscale_safe(
            detect_gray,
            scale_factor=1.1,
            min_neighbors=self.detect_min_neighbors,
            min_size=self.detect_min_size,
        )
        if len(faces) == 0:
            # Softer pass for soft MJPEG; avoid scaleFactor < 1.1 (OpenCV 4.11 scaleIdx crash).
            faces = self._detect_multiscale_safe(
                detect_gray,
                scale_factor=1.1,
                min_neighbors=self.detect_fallback_neighbors,
                min_size=max(40, self.detect_min_size - 12),
            )

        if scale_back != 1.0 and len(faces) > 0:
            inv = 1.0 / scale_back
            faces = np.round(faces.astype(np.float64) * inv).astype(np.int32)

        boxes = [(int(x), int(y), int(w), int(h)) for x, y, w, h in faces]
        return self._filter_face_boxes(boxes, gray.shape)

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

        # minSize must be clearly smaller than the image or Haar pyramid asserts (scaleIdx).
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
                images.append(self._enhance_gray(face))
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

            if recognizer is not None and labels:
                face = self._normalize_face(frame, (x, y, w, h))
                label_id, raw_confidence = recognizer.predict(face)
                confidence = float(raw_confidence)
                if confidence <= self.recognition_threshold:
                    strict_name = labels.get(int(label_id), "Unknown")

                name, recognized, used_track, pending = self._apply_confirmation(
                    (x, y, w, h), strict_name, confidence, now
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
            }
            self._confirm_tracks.append(track)
        else:
            track["box"] = box
            track["seen_at"] = now

        stabilized = False
        pending = False

        if strict_name and strict_name not in {"Unknown", "Face"}:
            if track.get("candidate") == strict_name:
                track["streak"] = int(track.get("streak", 0)) + 1
            else:
                track["candidate"] = strict_name
                track["streak"] = 1
        else:
            track["streak"] = 0
            track["candidate"] = None

        if int(track.get("streak", 0)) >= self.confirm_frames:
            track["confirmed_name"] = str(track["candidate"])
        elif track.get("confirmed_name") and confidence is not None:
            # Grace: keep a previously confirmed identity through brief blur / turn.
            if confidence <= self.unknown_grace_threshold:
                stabilized = True
            else:
                track["confirmed_name"] = None
                track["streak"] = 0

        confirmed = track.get("confirmed_name")
        if confirmed:
            return str(confirmed), True, stabilized, False

        if strict_name and strict_name not in {"Unknown", "Face"}:
            pending = int(track.get("streak", 0)) > 0
            return strict_name, False, False, pending

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
        gray = self._frame_gray(frame)
        face = gray[y : y + h, x : x + w]
        return cv2.resize(face, FACE_SIZE)

    def _create_recognizer(self) -> Any | None:
        if not hasattr(cv2, "face"):
            return None
        return cv2.face.LBPHFaceRecognizer_create()

    def _person_id(self, name: str) -> str:
        cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "_", name.strip().lower())
        return cleaned.strip("_")

    def _display_name(self, person_id: str) -> str:
        return person_id.replace("_", " ").title()
