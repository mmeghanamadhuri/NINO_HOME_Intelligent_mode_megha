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
MAX_GLOBAL_MODEL_BYTES = 50 * 1024 * 1024
CALIBRATION_PREDICT_MAX = 24
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
        self._person_recognizers: dict[int, Any] = {}
        self._person_thresholds: dict[int, float] = {}
        self.person_thresholds_path = data_dir / "person_thresholds.json"
        self.person_lbph_dir = data_dir / "person_lbph"
        self.person_lbph_meta_path = data_dir / "person_lbph_meta.json"
        self._confirm_tracks: list[dict[str, Any]] = []
        self._session_primary_name: str | None = None
        self._session_primary_at: float = 0.0
        self._person_models_ready = threading.Event()
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
        self.person_lbph_dir.mkdir(parents=True, exist_ok=True)
        self._load_model()
        self._init_yunet_detector()
        self._person_models_ready.set()

    def apply_settings_from_environ(self) -> None:
        """Reload tunables from environment (also used at startup / CLI)."""
        self.recognition_threshold = float(os.environ.get("FACE_RECOGNITION_THRESHOLD", "62"))
        self.soft_recognition_threshold = float(
            os.environ.get("FACE_SOFT_RECOGNITION_THRESHOLD", "76")
        )
        self.recognition_margin_min = float(
            os.environ.get("FACE_RECOGNITION_MARGIN", "10")
        )
        self.excellent_match_max = float(os.environ.get("FACE_EXCELLENT_MATCH_MAX", "44"))
        grace_delta = float(os.environ.get("FACE_UNKNOWN_GRACE_DELTA", "12"))
        self.unknown_grace_threshold = self.recognition_threshold + grace_delta
        self.track_hold_seconds = float(os.environ.get("FACE_TRACK_HOLD_SECONDS", "8.0"))
        self.track_match_iou = float(os.environ.get("FACE_TRACK_MATCH_IOU", "0.18"))
        self.confirm_frames = max(1, int(os.environ.get("FACE_CONFIRM_FRAMES", "2")))
        self.weak_confirm_frames = max(
            self.confirm_frames, int(os.environ.get("FACE_WEAK_CONFIRM_FRAMES", "3"))
        )
        self.session_primary_hold_seconds = float(
            os.environ.get("FACE_SESSION_PRIMARY_HOLD_SECONDS", "90")
        )
        self.secondary_face_area_ratio = float(
            os.environ.get("FACE_SECONDARY_AREA_RATIO", "0.40")
        )
        self.identity_sample_frames = max(
            3, int(os.environ.get("FACE_IDENTITY_SAMPLE_FRAMES", "5"))
        )
        self.identity_sample_gap_s = float(
            os.environ.get("FACE_IDENTITY_SAMPLE_GAP_S", "0.06")
        )
        self.detect_min_neighbors = int(os.environ.get("FACE_DETECT_MIN_NEIGHBORS", "3"))
        self.detect_fallback_neighbors = int(os.environ.get("FACE_DETECT_FALLBACK_NEIGHBORS", "2"))
        self.detect_min_size = int(os.environ.get("FACE_DETECT_MIN_SIZE", "24"))
        self.yunet_score_threshold = float(os.environ.get("FACE_YUNET_SCORE", "0.38"))
        self.face_pad_ratio = float(os.environ.get("FACE_CROP_PAD_RATIO", "0.30"))
        self.min_face_area_ratio = float(os.environ.get("FACE_MIN_AREA_RATIO", "0.0010"))
        self.min_face_aspect = float(os.environ.get("FACE_MIN_ASPECT", "0.68"))
        self.max_face_aspect = float(os.environ.get("FACE_MAX_ASPECT", "1.42"))
        self.max_samples_per_person = max(
            8, int(os.environ.get("FACE_MAX_SAMPLES_PER_PERSON", "36"))
        )

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
            "margin_min": self.recognition_margin_min,
            "unknown_grace_threshold": self.unknown_grace_threshold,
            "confirm_frames": self.confirm_frames,
            "weak_confirm_frames": self.weak_confirm_frames,
            "track_hold_seconds": self.track_hold_seconds,
            "detect_min_neighbors": self.detect_min_neighbors,
            "detector": "yunet" if self._yunet_enabled else "haar",
            "session_primary_hold_seconds": self.session_primary_hold_seconds,
            "secondary_face_area_ratio": self.secondary_face_area_ratio,
        }

    @staticmethod
    def _box_area(box: tuple[int, int, int, int] | dict[str, Any]) -> int:
        if isinstance(box, dict):
            return int(box.get("w", 0)) * int(box.get("h", 0))
        _x, _y, w, h = box
        return int(w) * int(h)

    @staticmethod
    def _is_known_name(name: str | None) -> bool:
        if not name:
            return False
        return str(name).strip().lower() not in {"unknown", "face", ""}

    def _session_primary_hint(self) -> str | None:
        if not self._session_primary_name:
            return None
        if (time.time() - self._session_primary_at) > self.session_primary_hold_seconds:
            return None
        return self._session_primary_name

    def _update_session_primary(self, name: str | None) -> None:
        if not self._is_known_name(name):
            return
        self._session_primary_name = str(name).strip()
        self._session_primary_at = time.time()

    def primary_viewer(self, results: list[dict[str, Any]]) -> str | None:
        """Largest face in frame with a confident identity (closest viewer)."""
        best_name: str | None = None
        best_area = 0
        for result in results:
            if not result.get("recognized") and not result.get("stabilized"):
                continue
            if not result.get("primary", True):
                continue
            name = str(result.get("name", "")).strip()
            if not self._is_known_name(name):
                continue
            area = self._box_area(result.get("box") or {})
            if area > best_area:
                best_area = area
                best_name = name
        return best_name

    def recognize_identity(
        self, read_frame: Any, *, samples: int | None = None
    ) -> tuple[str | None, str]:
        """Multi-frame vote for voice identity ('who am I?'). Returns (name, state)."""
        sample_count = samples if samples is not None else self.identity_sample_frames
        votes: dict[str, int] = {}
        saw_face = False

        for _ in range(sample_count):
            frame = read_frame()
            if frame is None:
                time.sleep(self.identity_sample_gap_s)
                continue

            results = self.recognize(frame)
            if not results:
                time.sleep(self.identity_sample_gap_s)
                continue

            saw_face = True
            primary = self.primary_viewer(results)
            if primary:
                votes[primary] = votes.get(primary, 0) + 1
            time.sleep(self.identity_sample_gap_s)

        if not saw_face:
            hint = self._session_primary_hint()
            if hint:
                return hint, "recognized"
            return None, "no_face"

        if votes:
            winner = max(votes, key=lambda key: votes[key])
            if votes[winner] >= max(2, (sample_count + 1) // 2):
                return winner, "recognized"

        hint = self._session_primary_hint()
        if hint:
            return hint, "recognized"

        return None, "unknown"

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
        if max_side > 1280:
            scale_back = 1280.0 / max_side
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
        if face is None:
            raise ValueError("Face is partially outside the frame — recenter and retry")

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

            sample_paths = self._select_sample_paths(person_dir)
            if not sample_paths:
                continue

            labels[label_id] = self._display_name(person_dir.name)
            for sample_path in sample_paths:
                image = cv2.imread(str(sample_path), cv2.IMREAD_GRAYSCALE)
                if image is None:
                    continue
                face = cv2.resize(image, FACE_SIZE)
                for variant in self._augment_training_face(face, lite=False):
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
            self._rebuild_person_recognizers_locked()

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
            person_recs = dict(self._person_recognizers)

        session_hint = self._session_primary_hint()
        sorted_faces = sorted(faces, key=self._box_area, reverse=True)
        largest_area = self._box_area(sorted_faces[0]) if sorted_faces else 0

        for index, (x, y, w, h) in enumerate(sorted_faces):
            box = (x, y, w, h)
            face_area = self._box_area(box)
            is_primary = index == 0 or (
                largest_area > 0
                and face_area >= int(largest_area * self.secondary_face_area_ratio)
            )

            name = "Face"
            confidence: float | None = None
            recognized = False
            used_track = False
            pending = False
            strict_name: str | None = None
            weak_name: str | None = None

            face = (
                self._normalize_face(frame, box)
                if recognizer is not None and labels
                else None
            )
            if face is not None:
                strict_name, weak_name, confidence, margin_ok = self._classify_face(
                    face, labels, person_recs, session_hint=session_hint
                )

                if not is_primary and strict_name:
                    weak_name = strict_name
                    strict_name = None
                    margin_ok = False

                display_name, used_track, pending_track = self._apply_confirmation(
                    box,
                    strict_name,
                    weak_name,
                    confidence,
                    now,
                    margin_ok,
                    session_hint=session_hint,
                )
                recognized = strict_name is not None or (
                    used_track and self._is_known_name(display_name)
                )
                pending = (
                    not recognized
                    and self._is_known_name(display_name)
                    and (weak_name is not None or pending_track)
                )
                if recognized:
                    name = display_name if self._is_known_name(display_name) else (
                        strict_name or display_name
                    )
                elif pending:
                    name = display_name
                else:
                    name = display_name

            results.append(
                {
                    "box": {"x": x, "y": y, "w": w, "h": h},
                    "name": name,
                    "recognized": recognized,
                    "confidence": confidence,
                    "stabilized": used_track,
                    "pending": pending,
                    "primary": is_primary,
                }
            )

        primary_name = self.primary_viewer(results)
        if primary_name:
            self._update_session_primary(primary_name)

        return results

    def annotate(self, frame: np.ndarray) -> tuple[np.ndarray, list[dict[str, Any]]]:
        output = frame.copy()
        results = self.recognize(frame)

        for result in results:
            box = result["box"]
            x, y, w, h = box["x"], box["y"], box["w"], box["h"]
            if result["recognized"] or result.get("stabilized"):
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
        margin_ok: bool = True,
        *,
        session_hint: str | None = None,
    ) -> tuple[str, bool, bool]:
        """Returns (display_name, stabilized, pending_track)."""
        track = self._match_confirm_track(box, now)
        is_new_track = track is None
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

        confirmed_name = track.get("confirmed_name")
        if confirmed_name and strict_name and strict_name != confirmed_name:
            if margin_ok or confidence is None or confidence <= self.unknown_grace_threshold:
                track["confirmed_name"] = None
                track["streak"] = 0
                track["candidate"] = None
                track["weak_streak"] = False
                confirmed_name = None

        active_name = strict_name
        confirm_needed = self.confirm_frames
        is_weak = False
        if not active_name and weak_name:
            active_name = weak_name
            confirm_needed = self.weak_confirm_frames
            is_weak = True

        if (
            is_new_track
            and session_hint
            and active_name == session_hint
            and self._is_known_name(active_name)
        ):
            track["candidate"] = active_name
            track["weak_streak"] = is_weak
            track["streak"] = max(1, confirm_needed - 1)

        if active_name and self._is_known_name(active_name):
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

        if int(track.get("streak", 0)) >= confirm_needed and (
            not is_weak or (margin_ok and int(track.get("streak", 0)) >= self.weak_confirm_frames)
        ):
            track["confirmed_name"] = str(track["candidate"])
        elif track.get("confirmed_name") and confidence is not None:
            if confidence > self.unknown_grace_threshold:
                track["confirmed_name"] = None
                track["streak"] = 0

        confirmed = track.get("confirmed_name")
        if confirmed:
            return str(confirmed), True, False

        if active_name and self._is_known_name(active_name):
            return active_name, False, int(track.get("streak", 0)) > 0

        if confirmed_name and self._is_known_name(str(confirmed_name)):
            return str(confirmed_name), True, False

        return "Unknown", False, False

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

    def _classify_face(
        self,
        face: np.ndarray,
        labels: dict[int, str],
        person_recs: dict[int, Any] | None = None,
        *,
        session_hint: str | None = None,
    ) -> tuple[str | None, str | None, float | None, bool]:
        """Return (strict_name, weak_name, best_lbph_distance, margin_ok)."""
        ranked = self._predict_ranked(face, labels, person_recs)
        if not ranked:
            if self._recognizer is None:
                return None, None, None, False
            label_id, raw_confidence = self._recognizer.predict(face)
            confidence = float(raw_confidence)
            best_name = labels.get(int(label_id), "Unknown")
            if confidence <= self.recognition_threshold:
                return best_name, None, confidence, True
            if confidence <= self.soft_recognition_threshold:
                return None, best_name, confidence, True
            return None, None, confidence, False

        best_conf, best_id, best_name = ranked[0]
        second_conf = ranked[1][0] if len(ranked) > 1 else best_conf + self.recognition_margin_min
        margin = second_conf - best_conf
        margin_ok = margin >= self.recognition_margin_min
        relaxed_margin_ok = margin >= max(4.0, self.recognition_margin_min * 0.5)
        excellent = best_conf <= self.excellent_match_max and margin >= max(
            6.0, self.recognition_margin_min * 0.6
        )
        person_cap = self._person_thresholds.get(
            int(best_id), self.recognition_threshold
        )
        accept_cap = max(self.recognition_threshold, person_cap)

        strict_name: str | None = None
        weak_name: str | None = None
        if (margin_ok or excellent) and best_conf <= accept_cap:
            strict_name = best_name
        elif (margin_ok or excellent) and best_conf <= self.soft_recognition_threshold:
            weak_name = best_name
        elif (
            session_hint
            and best_name == session_hint
            and best_conf <= self.soft_recognition_threshold
            and (relaxed_margin_ok or excellent)
        ):
            strict_name = best_name
            margin_ok = True
        elif (
            session_hint
            and best_name == session_hint
            and best_conf <= self.soft_recognition_threshold
        ):
            weak_name = best_name
        return strict_name, weak_name, best_conf, margin_ok or excellent

    def _predict_ranked(
        self,
        face: np.ndarray,
        labels: dict[int, str],
        person_recs: dict[int, Any] | None = None,
    ) -> list[tuple[float, int, str]]:
        recs = person_recs if person_recs is not None else self._person_recognizers
        ranked: list[tuple[float, int, str]] = []
        for label_id, name in labels.items():
            rec = recs.get(int(label_id))
            if rec is None:
                continue
            try:
                _, conf = rec.predict(face)
            except cv2.error:
                continue
            ranked.append((float(conf), int(label_id), name))
        ranked.sort(key=lambda item: item[0])
        return ranked

    def _select_sample_paths(self, person_dir: Path) -> list[Path]:
        """Use the newest N photos so hundreds of old captures do not slow startup."""
        paths = sorted(person_dir.glob("*.jpg"), key=lambda p: p.stat().st_mtime, reverse=True)
        return paths[: self.max_samples_per_person]

    def _person_fingerprint(self, person_dir: Path) -> tuple[int, float]:
        paths = list(person_dir.glob("*.jpg"))
        if not paths:
            return (0, 0.0)
        return (len(paths), max(p.stat().st_mtime for p in paths))

    def _load_person_thresholds_file(self) -> None:
        if not self.person_thresholds_path.is_file():
            return
        raw = json.loads(self.person_thresholds_path.read_text(encoding="utf-8"))
        self._person_thresholds = {int(k): float(v) for k, v in raw.items()}

    def _collect_person_training_images(
        self, person_dir: Path, *, lite: bool
    ) -> list[np.ndarray]:
        images: list[np.ndarray] = []
        for sample_path in self._select_sample_paths(person_dir):
            image = cv2.imread(str(sample_path), cv2.IMREAD_GRAYSCALE)
            if image is None:
                continue
            face = cv2.resize(image, FACE_SIZE)
            for variant in self._augment_training_face(face, lite=lite):
                images.append(self._enhance_gray(variant))
        return images

    def _calibrate_person_cap(self, rec: Any, images: list[np.ndarray]) -> float:
        if len(images) < 3:
            return self.recognition_threshold
        step = max(1, len(images) // CALIBRATION_PREDICT_MAX)
        dists: list[float] = []
        for sample in images[::step]:
            try:
                _, conf = rec.predict(sample)
                dists.append(float(conf))
            except cv2.error:
                continue
        if not dists:
            return self.recognition_threshold
        cap = float(np.percentile(dists, 88)) + 16.0
        return max(self.recognition_threshold, min(80.0, cap))

    def _try_load_person_lbph_cache(self) -> str:
        """Return 'full', 'partial', or 'miss'."""
        if not self.person_lbph_meta_path.is_file() or self._create_recognizer() is None:
            return "miss"
        try:
            meta = json.loads(self.person_lbph_meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return "miss"
        if meta.get("version") != 1:
            return "miss"
        people_meta: dict[str, Any] = meta.get("people", {})
        if not people_meta:
            return "miss"

        loaded: dict[int, Any] = {}
        need_ids: list[int] = []
        for label_id, display_name in self._labels.items():
            person_id = self._person_id(display_name)
            if not person_id:
                continue
            person_dir = self.faces_dir / person_id
            if not person_dir.is_dir():
                need_ids.append(int(label_id))
                continue
            fp = self._person_fingerprint(person_dir)
            entry = people_meta.get(person_id)
            model_file = self.person_lbph_dir / f"{int(label_id)}.yml"
            if not entry or entry.get("fingerprint") != list(fp) or not model_file.is_file():
                need_ids.append(int(label_id))
                continue
            rec = self._create_recognizer()
            if rec is None:
                return "miss"
            rec.read(str(model_file))
            loaded[int(label_id)] = rec

        if not loaded:
            return "miss"

        self._person_recognizers = loaded
        if need_ids:
            logger.info(
                "Loaded per-person LBPH cache (%d); rebuild needed for %d",
                len(loaded),
                len(need_ids),
            )
            return "partial"

        logger.info("Loaded per-person LBPH cache (%d people)", len(loaded))
        return "full"

    def _save_person_lbph_cache(self, meta_people: dict[str, Any]) -> None:
        self.person_lbph_meta_path.write_text(
            json.dumps({"version": 1, "people": meta_people}, indent=2),
            encoding="utf-8",
        )

    def _rebuild_person_recognizers_locked(self, *, only_missing: bool = False) -> None:
        """Per-person LBPH + calibrated distance caps (capped samples, lite aug)."""
        if not only_missing:
            self._person_recognizers = {}
        if self._create_recognizer() is None:
            return

        thresholds_out: dict[str, float] = {}
        if self.person_thresholds_path.is_file():
            try:
                raw = json.loads(self.person_thresholds_path.read_text(encoding="utf-8"))
                thresholds_out = {k: float(v) for k, v in raw.items()}
            except json.JSONDecodeError:
                pass
        meta_people: dict[str, Any] = {}
        if self.person_lbph_meta_path.is_file():
            try:
                existing = json.loads(self.person_lbph_meta_path.read_text(encoding="utf-8"))
                meta_people = dict(existing.get("people", {}))
            except json.JSONDecodeError:
                pass
        t0 = time.time()

        for label_id, display_name in self._labels.items():
            lid = int(label_id)
            if only_missing and lid in self._person_recognizers:
                continue
            person_id = self._person_id(display_name)
            if not person_id:
                continue
            person_dir = self.faces_dir / person_id
            if not person_dir.is_dir():
                continue

            images = self._collect_person_training_images(person_dir, lite=True)
            if len(images) < 3:
                continue

            rec = self._create_recognizer()
            if rec is None:
                continue
            rec.train(images, np.zeros(len(images), dtype=np.int32))
            self._person_recognizers[lid] = rec

            cap = self._calibrate_person_cap(rec, images)
            self._person_thresholds[lid] = cap
            thresholds_out[str(lid)] = round(cap, 1)

            model_file = self.person_lbph_dir / f"{lid}.yml"
            rec.write(str(model_file))
            meta_people[person_id] = {
                "label_id": lid,
                "fingerprint": list(self._person_fingerprint(person_dir)),
            }
            logger.info(
                "Built LBPH for %s (%d aug samples, cap %.1f)",
                display_name,
                len(images),
                cap,
            )

        if thresholds_out:
            self.person_thresholds_path.write_text(
                json.dumps(thresholds_out, indent=2), encoding="utf-8"
            )
        if meta_people:
            self._save_person_lbph_cache(meta_people)
        logger.info("Per-person models ready in %.1fs", time.time() - t0)

    def _load_model(self) -> None:
        if self._recognizer is None or not self.labels_path.exists():
            return

        labels_raw = json.loads(self.labels_path.read_text(encoding="utf-8"))
        self._labels = {int(key): value for key, value in labels_raw.items()}
        self._load_person_thresholds_file()

        cache_state = self._try_load_person_lbph_cache()
        if cache_state == "full":
            model_bytes = self.model_path.stat().st_size if self.model_path.exists() else 0
            if model_bytes > MAX_GLOBAL_MODEL_BYTES:
                logger.warning(
                    "face_model.yml is %d MB — skipped. Click Retrain to rebuild a small file.",
                    model_bytes // (1024 * 1024),
                )
            elif self.model_path.is_file():
                self._recognizer.read(str(self.model_path))
            return

        model_bytes = self.model_path.stat().st_size if self.model_path.is_file() else 0
        if self.model_path.is_file():
            if model_bytes > MAX_GLOBAL_MODEL_BYTES:
                logger.warning(
                    "face_model.yml is %d MB — skipped loading; click Retrain to rebuild.",
                    model_bytes // (1024 * 1024),
                )
            else:
                self._recognizer.read(str(self.model_path))

        self._rebuild_person_recognizers_locked(only_missing=(cache_state == "partial"))
        if not self._person_thresholds:
            self._load_person_thresholds_file()

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
    ) -> np.ndarray | None:
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
            face = gray[max(0, y) : min(fh, y + h), max(0, x) : min(fw, x + w)]
        if face.size == 0:
            # Detector box fell outside the frame (partial/stale frame edge).
            return None

        face = cv2.bilateralFilter(face, d=5, sigmaColor=28, sigmaSpace=28)

        min_side = min(face.shape[:2])
        target_min = 96 if min_side < 56 else 80
        if min_side < target_min:
            scale = float(target_min) / max(1, min_side)
            face = cv2.resize(
                face,
                (max(1, int(face.shape[1] * scale)), max(1, int(face.shape[0] * scale))),
                interpolation=cv2.INTER_CUBIC,
            )

        return cv2.resize(face, FACE_SIZE, interpolation=cv2.INTER_CUBIC)

    @staticmethod
    def _augment_training_face(face: np.ndarray, *, lite: bool = False) -> list[np.ndarray]:
        """Synthetic views for distance, blur, and lighting (ESP MJPEG stream)."""
        base = cv2.resize(face, FACE_SIZE)
        variants: list[np.ndarray] = []

        def add(img: np.ndarray) -> None:
            variants.append(img)

        scales = (0.65, 0.85, 1.0) if lite else (0.52, 0.65, 0.75, 0.86, 1.0, 1.08)
        for scale in scales:
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
        rot_angles = () if lite else (-8.0, -4.0, 4.0, 8.0)
        for v in variants:
            out.append(v)
            out.append(cv2.GaussianBlur(v, (0, 0), 0.85))
            if lite:
                out.append(cv2.convertScaleAbs(v, alpha=1.06, beta=8))
                continue
            out.append(cv2.convertScaleAbs(v, alpha=1.10, beta=10))
            out.append(cv2.convertScaleAbs(v, alpha=0.90, beta=-10))
            h, w = v.shape[:2]
            center = (w // 2, h // 2)
            for angle in rot_angles:
                rot = cv2.getRotationMatrix2D(center, angle, 1.0)
                out.append(
                    cv2.warpAffine(
                        v, rot, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE
                    )
                )
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
