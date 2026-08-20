from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import cv2
import numpy as np

logger = logging.getLogger(__name__)

# SFace expects an aligned 112x112 BGR crop.
SFACE_INPUT_SIZE = (112, 112)
YUNET_FILENAME = "face_detection_yunet_2023mar.onnx"
SFACE_FILENAME = "face_recognition_sface_2021dec.onnx"
OPENCV_ZOO_BASE = "https://github.com/opencv/opencv_zoo/raw/main/models"
YUNET_MODEL_URL = f"{OPENCV_ZOO_BASE}/face_detection_yunet/{YUNET_FILENAME}"
SFACE_MODEL_URL = f"{OPENCV_ZOO_BASE}/face_recognition_sface/{SFACE_FILENAME}"


class FaceService:
    """Deep-embedding face recognition (SFace), reference-project style.

    Each registered sample is converted once into a 128-D embedding; at runtime a
    detected face is embedded and matched by cosine similarity against the store.
    No LBPH training, no augmentation, no per-person thresholds.
    """

    def __init__(self, data_dir: Path, recognition_threshold: float | None = None) -> None:
        self.data_dir = data_dir
        self.faces_dir = data_dir / "faces"
        self.embeddings_path = data_dir / "face_embeddings.json"
        self._yunet_model_path = data_dir / "models" / YUNET_FILENAME
        self._sface_model_path = data_dir / "models" / SFACE_FILENAME

        self._lock = threading.Lock()
        self._state_lock = threading.Lock()
        # YuNet / SFace are not thread-safe; serialize all native model calls.
        self._model_lock = threading.RLock()
        # person_id -> (display_name, embeddings ndarray [n_samples, 128])
        self._embeddings: dict[str, tuple[str, np.ndarray]] = {}
        self._session_primary_name: str | None = None
        self._session_primary_at: float = 0.0
        self._primary_candidate_name: str | None = None
        self._primary_candidate_streak = 0
        self._primary_stable_name: str | None = None
        self._primary_stable_until: float = 0.0
        self._session_unknown_streak = 0

        if recognition_threshold is not None and recognition_threshold <= 1.0:
            os.environ["FACE_MATCH_THRESHOLD"] = str(recognition_threshold)
        self.apply_settings_from_environ()

        self._cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        )
        self._yunet: Any | None = None
        self._yunet_enabled = False
        self._sface: Any | None = None

        self.faces_dir.mkdir(parents=True, exist_ok=True)
        self._init_yunet_detector()
        self._init_sface_recognizer()
        self._load_embeddings()
        if not self._embeddings and any(self.faces_dir.glob("*/*.jpg")):
            # First run after the LBPH -> SFace migration: reuse stored samples.
            try:
                result = self.train()
                logger.info(
                    "Re-encoded existing samples: %d people, %d embeddings",
                    result["people"],
                    result["samples"],
                )
            except (ValueError, RuntimeError) as exc:
                logger.warning("Could not re-encode existing samples: %s", exc)

    def apply_settings_from_environ(self) -> None:
        """Reload tunables from environment (also used at startup / CLI)."""
        # Cosine similarity acceptance (SFace reference operating point: 0.363).
        self.match_threshold = float(os.environ.get("FACE_MATCH_THRESHOLD", "0.44"))
        legacy = os.environ.get("FACE_RECOGNITION_THRESHOLD")
        if legacy:
            try:
                value = float(legacy)
                if 0.0 < value <= 1.0:
                    self.match_threshold = value
                # Values > 1 are legacy LBPH distances — ignored.
            except ValueError:
                pass
        self.match_soft_threshold = float(
            os.environ.get(
                "FACE_MATCH_SOFT_THRESHOLD",
                f"{max(0.22, self.match_threshold - 0.02):.3f}",
            )
        )
        self.margin_min = float(os.environ.get("FACE_MATCH_MARGIN_MIN", "0.08"))
        self.confirm_frames = max(2, int(os.environ.get("FACE_CONFIRM_FRAMES", "5")))
        self.confirm_frames_switch = max(
            self.confirm_frames,
            int(os.environ.get("FACE_CONFIRM_FRAMES_SWITCH", "8")),
        )
        self.stable_hold_seconds = float(
            os.environ.get("FACE_STABLE_HOLD_SECONDS", "1.5")
        )
        self.embed_min_centroid_sim = float(
            os.environ.get("FACE_EMBED_MIN_CENTROID_SIM", "0.80")
        )
        self.recognition_min_detection_score = float(
            os.environ.get("FACE_RECOGNITION_MIN_DETECTION_SCORE", "0.72")
        )
        self.match_top_k = max(1, int(os.environ.get("FACE_MATCH_TOP_K", "3")))

        self.detect_min_size = int(os.environ.get("FACE_DETECT_MIN_SIZE", "32"))
        self.yunet_score_threshold = float(os.environ.get("FACE_YUNET_SCORE", "0.68"))
        self.haar_fallback = os.environ.get("FACE_HAAR_FALLBACK", "0").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self.landmark_validation = os.environ.get(
            "FACE_LANDMARK_VALIDATION", "1"
        ).strip().lower() in {"1", "true", "yes", "on"}
        self.min_face_area_ratio = float(os.environ.get("FACE_MIN_AREA_RATIO", "0.0025"))
        self.min_face_aspect = float(os.environ.get("FACE_MIN_ASPECT", "0.72"))
        self.max_face_aspect = float(os.environ.get("FACE_MAX_ASPECT", "1.38"))
        self.secondary_face_area_ratio = float(
            os.environ.get("FACE_SECONDARY_AREA_RATIO", "0.40")
        )
        self.max_detections = max(1, int(os.environ.get("FACE_MAX_DETECTIONS", "1")))
        self.session_primary_hold_seconds = float(
            os.environ.get("FACE_SESSION_PRIMARY_HOLD_SECONDS", "45")
        )
        self.session_clear_unknown_frames = max(
            3, int(os.environ.get("FACE_SESSION_CLEAR_UNKNOWN_FRAMES", "12"))
        )
        self.identity_sample_frames = max(
            3, int(os.environ.get("FACE_IDENTITY_SAMPLE_FRAMES", "5"))
        )
        self.identity_sample_gap_s = float(
            os.environ.get("FACE_IDENTITY_SAMPLE_GAP_S", "0.06")
        )
        self.max_samples_per_person = max(
            8, int(os.environ.get("FACE_MAX_SAMPLES_PER_PERSON", "60"))
        )

    # ------------------------------------------------------------------ status

    @property
    def recognizer_available(self) -> bool:
        return self._sface is not None

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

        with self._lock:
            trained = len(self._embeddings)
            embedded = int(sum(embs.shape[0] for _, embs in self._embeddings.values()))

        return {
            "recognizer_available": self.recognizer_available,
            "people": people,
            "trained_people": trained,
            "embedded_samples": embedded,
            "threshold": self.match_threshold,
            "soft_threshold": self.match_soft_threshold,
            "margin_min": self.margin_min,
            "confirm_frames": self.confirm_frames,
            "confirm_frames_switch": self.confirm_frames_switch,
            "embed_min_centroid_sim": self.embed_min_centroid_sim,
            "recognition_min_detection_score": self.recognition_min_detection_score,
            "match_top_k": self.match_top_k,
            "engine": "sface" if self._sface is not None else "unavailable",
            "detector": "yunet" if self._yunet_enabled else "haar",
            "yunet_score_threshold": self.yunet_score_threshold,
            "haar_fallback": self.haar_fallback,
            "landmark_validation": self.landmark_validation,
            "max_detections": self.max_detections,
            "session_primary_hold_seconds": self.session_primary_hold_seconds,
            "secondary_face_area_ratio": self.secondary_face_area_ratio,
        }

    # --------------------------------------------------------------- detection

    def detect(self, frame: np.ndarray) -> list[tuple[int, int, int, int]]:
        return [box for box, _row in self._detect_faces(frame)]

    def _detect_faces(
        self, frame: np.ndarray
    ) -> list[tuple[tuple[int, int, int, int], np.ndarray | None]]:
        """Returns [(box, yunet_row_or_None)]. The row carries landmarks for alignment."""
        if frame is None or frame.size == 0:
            return []
        h, w = frame.shape[:2]
        if h < 32 or w < 32:
            return []

        detections: list[tuple[tuple[int, int, int, int], np.ndarray | None]] = []
        if self._yunet_enabled and self._yunet is not None:
            detections = self._detect_yunet(frame)
        if not detections and self.haar_fallback:
            detections = [(box, None) for box in self._detect_haar(frame)]

        filtered = self._filter_detections(detections, (h, w))
        if self.landmark_validation:
            filtered = [
                item
                for item in filtered
                if self._detection_quality_ok(item[0], item[1])[0]
            ]
        if len(filtered) <= self.max_detections:
            return filtered

        def _det_rank(item: tuple[tuple[int, int, int, int], np.ndarray | None]) -> float:
            (_x, _y, bw, bh), row = item
            area = float(max(1, bw * bh))
            score = float(row[14]) if row is not None and len(row) > 14 else 1.0
            return area * score

        filtered.sort(key=_det_rank, reverse=True)
        return filtered[: self.max_detections]

    def _detect_yunet(
        self, frame: np.ndarray
    ) -> list[tuple[tuple[int, int, int, int], np.ndarray | None]]:
        assert self._yunet is not None
        h, w = frame.shape[:2]
        bgr = frame if frame.ndim == 3 else cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
        try:
            with self._model_lock:
                self._yunet.setInputSize((w, h))
                _, faces = self._yunet.detect(bgr)
        except cv2.error:
            return []

        if faces is None or len(faces) == 0:
            return []

        detections: list[tuple[tuple[int, int, int, int], np.ndarray | None]] = []
        for row in faces:
            if len(row) < 15:
                continue
            score = float(row[14])
            if not np.isfinite(score) or score < self.yunet_score_threshold:
                continue
            if not np.isfinite(row[:4]).all():
                # Guard against rare detector glitches producing inf/nan boxes.
                continue
            x, y, bw, bh = int(row[0]), int(row[1]), int(row[2]), int(row[3])
            if bw <= 0 or bh <= 0:
                continue
            detections.append(((x, y, bw, bh), np.asarray(row, dtype=np.float32)))
        return detections

    def _detect_haar(self, frame: np.ndarray) -> list[tuple[int, int, int, int]]:
        gray = frame if frame.ndim == 2 else cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
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

        ms = max(24, min(self.detect_min_size, min(detect_gray.shape[:2]) - 8))
        try:
            found = self._cascade.detectMultiScale(
                detect_gray,
                scaleFactor=1.1,
                minNeighbors=6,
                minSize=(ms, ms),
                flags=cv2.CASCADE_SCALE_IMAGE,
            )
        except cv2.error:
            return []
        if found is None or len(found) == 0:
            return []

        faces = np.asarray(found, dtype=np.float64)
        if scale_back != 1.0:
            faces = np.round(faces / scale_back)
        return [(int(x), int(y), int(bw), int(bh)) for x, y, bw, bh in faces]

    def _filter_detections(
        self,
        detections: list[tuple[tuple[int, int, int, int], np.ndarray | None]],
        frame_shape: tuple[int, int],
    ) -> list[tuple[tuple[int, int, int, int], np.ndarray | None]]:
        fh, fw = frame_shape
        frame_area = max(1, fh * fw)
        kept: list[tuple[tuple[int, int, int, int], np.ndarray | None]] = []
        for (x, y, w, h), row in detections:
            if w <= 0 or h <= 0:
                continue
            if (w * h) / frame_area < self.min_face_area_ratio:
                continue
            aspect = w / float(h)
            if aspect < self.min_face_aspect or aspect > self.max_face_aspect:
                continue
            kept.append(((x, y, w, h), row))
        return kept

    @staticmethod
    def _landmarks_plausible(
        row: np.ndarray, x: int, y: int, w: int, h: int
    ) -> bool:
        """Reject YuNet boxes on walls/textures — landmarks must look like a real face."""
        if row is None or len(row) < 15:
            return False
        if w <= 0 or h <= 0:
            return False

        pts = [
            (float(row[4]), float(row[5])),   # right eye
            (float(row[6]), float(row[7])),   # left eye
            (float(row[8]), float(row[9])),   # nose
            (float(row[10]), float(row[11])),  # right mouth corner
            (float(row[12]), float(row[13])),  # left mouth corner
        ]
        if not all(np.isfinite(px) and np.isfinite(py) for px, py in pts):
            return False

        margin = max(2.0, 0.08 * float(min(w, h)))
        for px, py in pts:
            if px < x - margin or px > x + w + margin:
                return False
            if py < y - margin or py > y + h + margin:
                return False

        re_x, re_y = pts[0]
        le_x, le_y = pts[1]
        nose_x, nose_y = pts[2]
        rcm_x, rcm_y = pts[3]
        lcm_x, lcm_y = pts[4]

        eye_dy = abs(re_y - le_y)
        if eye_dy > 0.22 * h:
            return False

        eye_y = (re_y + le_y) * 0.5
        if eye_y < y + 0.12 * h or eye_y > y + 0.58 * h:
            return False

        eye_dx = abs(le_x - re_x)
        if eye_dx < 0.22 * w or eye_dx > 0.82 * w:
            return False

        if nose_y < eye_y + 0.06 * h or nose_y > y + 0.82 * h:
            return False

        mouth_y = (rcm_y + lcm_y) * 0.5
        if mouth_y < nose_y + 0.04 * h or mouth_y > y + 0.98 * h:
            return False

        if nose_x < x + 0.18 * w or nose_x > x + 0.82 * w:
            return False

        if mouth_y < y + 0.48 * h:
            return False

        return True

    def _detection_quality_ok(
        self, box: tuple[int, int, int, int], row: np.ndarray | None
    ) -> tuple[bool, float]:
        """Return (passes_quality_gate, detector_score)."""
        x, y, w, h = box
        if row is None:
            # Haar (no landmarks) is too noisy for live overlay / registration.
            return False, 0.0

        score = float(row[14]) if len(row) > 14 else 0.0
        if not np.isfinite(score) or score < self.yunet_score_threshold:
            return False, score

        if self.landmark_validation and not self._landmarks_plausible(row, x, y, w, h):
            return False, score

        return True, score

    def _registration_eligible(
        self,
        *,
        detection_valid: bool,
        recognized: bool,
        stabilized: bool,
        candidate_score: float | None,
        candidate_name: str | None,
    ) -> bool:
        """True when the primary face is shown as Unknown and may be voice-registered.

        Only block near soft-threshold matches (about to stabilize as known). Weaker
        partial scores (e.g. 0.34 vs Dimple) still prompt — capture-time
        validate_registration_name blocks renaming an already-enrolled face.
        """
        del candidate_name  # reserved for callers; soft-threshold score is enough
        if not detection_valid or recognized or stabilized:
            return False
        # Soft-threshold and above ≈ nearly identified; wait for recognition instead.
        if candidate_score is not None and candidate_score >= self.match_soft_threshold:
            return False
        return True

    # --------------------------------------------------------------- embedding

    def _aligned_crop(
        self, frame: np.ndarray, box: tuple[int, int, int, int], row: np.ndarray | None
    ) -> np.ndarray | None:
        """112x112 BGR crop for SFace — landmark-aligned when YuNet gave us a row."""
        bgr = frame if frame.ndim == 3 else cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)

        if row is not None and self._sface is not None:
            try:
                with self._model_lock:
                    aligned = self._sface.alignCrop(bgr, row)
                if aligned is not None and aligned.size > 0:
                    return aligned
            except cv2.error:
                pass

        x, y, w, h = box
        fh, fw = bgr.shape[:2]
        pad = int(max(w, h) * 0.15)
        x1, y1 = max(0, x - pad), max(0, y - pad)
        x2, y2 = min(fw, x + w + pad), min(fh, y + h + pad)
        crop = bgr[y1:y2, x1:x2]
        if crop.size == 0:
            return None
        return cv2.resize(crop, SFACE_INPUT_SIZE, interpolation=cv2.INTER_CUBIC)

    def _embed(self, aligned_bgr: np.ndarray) -> np.ndarray | None:
        if self._sface is None:
            return None
        try:
            with self._model_lock:
                feature = self._sface.feature(aligned_bgr)
        except cv2.error:
            return None
        vec = np.asarray(feature, dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(vec))
        if norm <= 0.0:
            return None
        return vec / norm

    def _embed_image_file(self, path: Path) -> np.ndarray | None:
        image = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if image is None or image.size == 0:
            return None
        # Stored samples are face crops; try to re-detect for landmark alignment.
        detections = self._detect_faces(image)
        if detections:
            box, row = max(detections, key=lambda d: d[0][2] * d[0][3])
            aligned = self._aligned_crop(image, box, row)
        else:
            aligned = cv2.resize(image, SFACE_INPUT_SIZE, interpolation=cv2.INTER_CUBIC)
        if aligned is None:
            return None
        return self._embed(aligned)

    @staticmethod
    def _normalize_embeddings(embs: np.ndarray) -> np.ndarray:
        if embs.ndim != 2 or embs.shape[0] == 0:
            return embs
        norms = np.linalg.norm(embs, axis=1, keepdims=True)
        norms = np.maximum(norms, 1e-6)
        return (embs / norms).astype(np.float32)

    def _prune_embeddings(self, embs: np.ndarray) -> np.ndarray:
        """Drop outlier samples that widen a person's match cone (reduces ghost IDs)."""
        if embs.ndim != 2 or embs.shape[0] <= 2:
            return embs.astype(np.float32)
        norms = self._normalize_embeddings(embs)
        centroid = norms.mean(axis=0)
        cn = float(np.linalg.norm(centroid))
        if cn <= 0.0:
            return norms
        centroid /= cn
        sims = norms @ centroid
        kept = norms[sims >= self.embed_min_centroid_sim]
        if kept.shape[0] == 0:
            kept = norms[[int(np.argmax(sims))]]
        return kept.astype(np.float32)

    def _person_match_score(self, embs: np.ndarray, query: np.ndarray) -> float:
        """Robust score: blend top-k sample mean with centroid (resists outlier steals)."""
        norms = self._normalize_embeddings(embs)
        sims = norms @ query
        k = min(self.match_top_k, sims.shape[0])
        top_mean = float(np.mean(np.partition(sims, -k)[-k:]))
        centroid = norms.mean(axis=0)
        cn = float(np.linalg.norm(centroid))
        if cn <= 0.0:
            return top_mean
        centroid /= cn
        centroid_sim = float(centroid @ query)
        return 0.45 * top_mean + 0.55 * centroid_sim

    def _match_embedding(self, embedding: np.ndarray) -> tuple[str | None, float, float]:
        """Returns (best_name, best_score, second_best_score) across all people."""
        best_name: str | None = None
        best_score = -1.0
        second_best_score = -1.0
        with self._lock:
            for _person_id, (display_name, embs) in self._embeddings.items():
                if embs.size == 0:
                    continue
                score = self._person_match_score(embs, embedding)
                if score > best_score:
                    second_best_score = best_score
                    best_score = score
                    best_name = display_name
                elif score > second_best_score:
                    second_best_score = score
        return best_name, best_score, second_best_score

    @staticmethod
    def _largest_primary_index(results: list[dict[str, Any]]) -> int | None:
        best_idx: int | None = None
        best_area = 0
        for idx, result in enumerate(results):
            if not result.get("primary", True):
                continue
            box = result.get("box") or {}
            area = int(box.get("w", 0)) * int(box.get("h", 0))
            if area > best_area:
                best_area = area
                best_idx = idx
        return best_idx

    def _stabilize_primary_face(self, results: list[dict[str, Any]]) -> None:
        """Require consistent primary recognition before treating it as known."""
        primary_idx = self._largest_primary_index(results)
        now = time.time()
        if primary_idx is None:
            with self._state_lock:
                self._primary_candidate_name = None
                self._primary_candidate_streak = 0
                if now > self._primary_stable_until:
                    self._primary_stable_name = None
            return

        primary = results[primary_idx]
        candidate_name = str(primary.get("candidate_name") or "").strip()
        candidate_score = float(primary.get("candidate_score") or 0.0)
        margin_score = float(primary.get("margin") or 0.0)
        raw_ok = bool(primary.get("raw_recognized"))

        with self._state_lock:
            required_confirm = self.confirm_frames
            if (
                self._primary_stable_name
                and candidate_name
                and candidate_name != self._primary_stable_name
            ):
                required_confirm = self.confirm_frames_switch

            if raw_ok and self._is_known_name(candidate_name):
                if candidate_name == self._primary_candidate_name:
                    self._primary_candidate_streak += 1
                else:
                    self._primary_candidate_name = candidate_name
                    self._primary_candidate_streak = 1
            else:
                self._primary_candidate_name = None
                self._primary_candidate_streak = 0

            stabilized = False
            if (
                self._primary_candidate_name is not None
                and self._primary_candidate_streak >= required_confirm
                and candidate_score >= self.match_threshold
                and margin_score >= self.margin_min
            ):
                self._primary_stable_name = self._primary_candidate_name
                self._primary_stable_until = now + self.stable_hold_seconds
                stabilized = True
            elif (
                self._primary_stable_name
                and candidate_name == self._primary_stable_name
                and candidate_score >= self.match_soft_threshold
                and margin_score >= self.margin_min
            ):
                # Hysteresis only while the same person stays clearly ahead.
                self._primary_stable_until = now + self.stable_hold_seconds
                stabilized = True
            elif now > self._primary_stable_until:
                self._primary_stable_name = None

            if stabilized and self._primary_stable_name:
                primary["name"] = self._primary_stable_name
                primary["recognized"] = True
                primary["stabilized"] = True
                primary["pending"] = False
            elif raw_ok:
                primary["recognized"] = False
                primary["stabilized"] = False
                primary["pending"] = True
                primary["name"] = f"{candidate_name} [hold]"
            else:
                primary["recognized"] = False
                primary["stabilized"] = False
                primary["pending"] = False
                primary["name"] = "Unknown"

    # ------------------------------------------------------------ registration

    def register_sample(self, name: str, frame: np.ndarray) -> Path:
        person_id = self._person_id(name)
        if not person_id:
            raise ValueError("Person name is required")

        if frame is None or frame.size == 0:
            raise ValueError("Invalid camera frame")
        fh, fw = frame.shape[:2]
        if fh < 60 or fw < 60:
            raise ValueError("Camera frame too small — wait for the stream to connect")

        detections = self._detect_faces(frame)
        if not detections:
            raise ValueError("No face detected in frame")

        box, row = max(detections, key=lambda d: d[0][2] * d[0][3])
        x, y, w, h = box
        bgr = frame if frame.ndim == 3 else cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
        pad = int(max(w, h) * 0.25)
        x1, y1 = max(0, x - pad), max(0, y - pad)
        x2, y2 = min(fw, x + w + pad), min(fh, y + h + pad)
        face_crop = bgr[y1:y2, x1:x2]
        if face_crop.size == 0:
            raise ValueError("Face is partially outside the frame — recenter and retry")

        person_dir = self.faces_dir / person_id
        person_dir.mkdir(parents=True, exist_ok=True)
        output_path = person_dir / f"{int(time.time() * 1000)}.jpg"
        cv2.imwrite(str(output_path), face_crop)

        # Encode immediately so the new sample is matchable without a retrain.
        aligned = self._aligned_crop(frame, box, row)
        if aligned is not None:
            embedding = self._embed(aligned)
            if embedding is not None:
                self._append_embedding(
                    person_id,
                    self._display_name(person_id),
                    embedding,
                    persist=False,
                )

        return output_path

    def _append_embedding(
        self,
        person_id: str,
        display_name: str,
        embedding: np.ndarray,
        *,
        persist: bool = True,
    ) -> None:
        with self._lock:
            _, existing = self._embeddings.get(
                person_id,
                (display_name, np.empty((0, embedding.shape[0]), dtype=np.float32)),
            )
            stacked = np.vstack([existing, embedding[None, :]])
            if stacked.shape[0] > self.max_samples_per_person:
                stacked = stacked[-self.max_samples_per_person :]
            stacked = self._prune_embeddings(stacked)
            self._embeddings[person_id] = (display_name, stacked.astype(np.float32))
            if persist:
                self._save_embeddings_locked()

    def persist_embeddings(self) -> None:
        with self._lock:
            self._save_embeddings_locked()

    # ---------------------------------------------------------------- training

    def train(self) -> dict[str, Any]:
        """Re-encode stored samples into embeddings. Fast — no model training."""
        if self._sface is None:
            raise RuntimeError(
                "SFace recognizer unavailable — could not load "
                f"{SFACE_FILENAME} (check the data/models folder or network)."
            )

        # Hold the model lock for the whole rebuild so the live MJPEG thread
        # cannot call YuNet/SFace at the same time (native crash on Windows).
        with self._model_lock:
            new_store: dict[str, tuple[str, np.ndarray]] = {}
            total = 0
            for person_dir in sorted(self.faces_dir.iterdir()):
                if not person_dir.is_dir():
                    continue
                sample_paths = sorted(
                    person_dir.glob("*.jpg"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )[: self.max_samples_per_person]
                vectors: list[np.ndarray] = []
                for sample_path in sample_paths:
                    embedding = self._embed_image_file(sample_path)
                    if embedding is not None:
                        vectors.append(embedding)
                if not vectors:
                    continue
                pruned = self._prune_embeddings(np.vstack(vectors).astype(np.float32))
                new_store[person_dir.name] = (
                    self._display_name(person_dir.name),
                    pruned,
                )
                total += pruned.shape[0]

            if not new_store:
                raise ValueError("No registered face samples found")

            with self._lock:
                self._embeddings = new_store
                self._save_embeddings_locked()

        return {"people": len(new_store), "samples": total}

    # ------------------------------------------------------------- recognition

    def recognize(self, frame: np.ndarray) -> list[dict[str, Any]]:
        detections = self._detect_faces(frame)
        results: list[dict[str, Any]] = []

        sorted_dets = sorted(detections, key=lambda d: d[0][2] * d[0][3], reverse=True)
        for index, (box, row) in enumerate(sorted_dets):
            x, y, w, h = box
            is_primary = index == 0
            detection_valid, detection_score = self._detection_quality_ok(box, row)

            name = "Unknown"
            confidence: float | None = None
            recognized = False
            best_name: str | None = None
            best_score = -1.0
            margin = 0.0

            if self._sface is not None:
                aligned = self._aligned_crop(frame, box, row)
                embedding = self._embed(aligned) if aligned is not None else None
                if (
                    embedding is not None
                    and detection_valid
                    and detection_score >= self.recognition_min_detection_score
                ):
                    best_name, best_score, second_best = self._match_embedding(embedding)
                    margin = best_score - max(second_best, -1.0)
                    confidence = round(best_score, 3)
                    if (
                        best_name is not None
                        and best_score >= self.match_threshold
                        and margin >= self.margin_min
                    ):
                        name = best_name
                        recognized = True
            else:
                name = "Face"

            registration_eligible = self._registration_eligible(
                detection_valid=detection_valid,
                recognized=recognized,
                stabilized=False,
                candidate_score=round(best_score, 3) if self._sface is not None else None,
                candidate_name=best_name,
            )

            results.append(
                {
                    "box": {"x": x, "y": y, "w": w, "h": h},
                    "name": name,
                    "recognized": recognized,
                    "confidence": confidence,
                    "stabilized": False,
                    "pending": False,
                    "candidate_name": best_name if self._sface is not None else None,
                    "candidate_score": round(best_score, 3) if self._sface is not None else None,
                    "margin": round(margin, 3) if self._sface is not None else None,
                    "raw_recognized": recognized,
                    "primary": is_primary,
                    "detection_valid": detection_valid,
                    "detection_score": round(detection_score, 3) if detection_valid else None,
                    "registration_eligible": registration_eligible,
                }
            )

        self._stabilize_primary_face(results)
        for result in results:
            if not result.get("stabilized", False):
                result["recognized"] = False
            result["registration_eligible"] = self._registration_eligible(
                detection_valid=bool(result.get("detection_valid")),
                recognized=bool(result.get("recognized")),
                stabilized=bool(result.get("stabilized")),
                candidate_score=result.get("candidate_score"),
                candidate_name=result.get("candidate_name"),
            )

        primary_name = self.primary_viewer(results)
        if primary_name:
            self._update_session_primary(primary_name)
            with self._state_lock:
                self._session_unknown_streak = 0
        else:
            with self._state_lock:
                if results and any(r.get("detection_valid") for r in results):
                    self._session_unknown_streak += 1
                    if self._session_unknown_streak >= self.session_clear_unknown_frames:
                        self._session_primary_name = None
                        self._session_primary_at = 0.0
                        self._primary_stable_name = None
                        self._primary_candidate_name = None
                        self._primary_candidate_streak = 0
                else:
                    self._session_unknown_streak = 0

        return results

    @staticmethod
    def _overlay_label_text(result: dict[str, Any]) -> str:
        label = str(result.get("name", ""))
        emotion = str(result.get("emotion") or "").strip()
        if emotion:
            label = f"{label} | {emotion}"
        if result.get("pending"):
            label = f"{label} [hold]"
        return label

    @staticmethod
    def _draw_overlay_label(
        frame: np.ndarray,
        *,
        label: str,
        x: int,
        y: int,
        w: int,
        h: int,
        accent_bgr: tuple[int, int, int],
    ) -> None:
        """Draw a centered name/emotion tag above the face box."""
        if not label:
            return

        fh, fw = frame.shape[:2]
        font = cv2.FONT_HERSHEY_SIMPLEX
        scale = float(max(0.42, min(0.58, w / 300.0)))
        thickness = max(1, int(round(scale * 2.2)))
        pad_x, pad_y = 8, 5
        gap = 8

        (text_w, text_h), baseline = cv2.getTextSize(label, font, scale, thickness)
        box_cx = x + w // 2
        text_x = box_cx - text_w // 2

        bg_w = text_w + pad_x * 2
        bg_h = text_h + pad_y * 2
        bg_x1 = text_x - pad_x
        bg_y2 = y - gap
        bg_y1 = bg_y2 - bg_h

        if bg_y1 < 2:
            bg_y1 = y + 4
            bg_y2 = bg_y1 + bg_h

        bg_x1 = max(2, min(bg_x1, fw - bg_w - 2))
        bg_x2 = bg_x1 + bg_w
        bg_y1 = max(2, min(bg_y1, fh - bg_h - 2))
        bg_y2 = bg_y1 + bg_h
        text_x = bg_x1 + pad_x
        text_y = bg_y2 - pad_y - max(1, baseline // 2)

        overlay = frame.copy()
        cv2.rectangle(overlay, (bg_x1, bg_y1), (bg_x2, bg_y2), (28, 28, 28), -1)
        cv2.addWeighted(overlay, 0.82, frame, 0.18, 0, frame)
        cv2.rectangle(frame, (bg_x1, bg_y1), (bg_x2, bg_y2), accent_bgr, 1, cv2.LINE_AA)
        cv2.putText(
            frame,
            label,
            (text_x, text_y),
            font,
            scale,
            (255, 255, 255),
            thickness,
            cv2.LINE_AA,
        )

    def annotate(
        self,
        frame: np.ndarray,
        *,
        results: list[dict[str, Any]] | None = None,
    ) -> tuple[np.ndarray, list[dict[str, Any]]]:
        output = frame.copy()
        if results is None:
            results = self.recognize(frame)

        for result in results:
            if not result.get("detection_valid", True):
                continue
            box = result["box"]
            x, y, w, h = box["x"], box["y"], box["w"], box["h"]
            color = (0, 200, 0) if result["recognized"] else (0, 180, 255)
            label = self._overlay_label_text(result)

            cv2.rectangle(output, (x, y), (x + w, y + h), color, 2, cv2.LINE_AA)
            self._draw_overlay_label(
                output,
                label=label,
                x=x,
                y=y,
                w=w,
                h=h,
                accent_bgr=color,
            )

        return output, results

    # ----------------------------------------------------- identity for voice

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

    def primary_viewer(
        self, results: list[dict[str, Any]], *, allow_pending: bool = False
    ) -> str | None:
        """Largest face in frame with a confident identity (closest viewer).

        ``allow_pending`` also accepts the overlay candidate (Hari [hold]) so a
        session greet can match what the browser already shows before the
        5-frame stabilizer commits.
        """
        best_name: str | None = None
        best_area = 0
        for result in results:
            if not result.get("primary", True):
                continue
            name = ""
            if result.get("recognized") or result.get("stabilized"):
                name = str(result.get("name", "")).strip()
            elif allow_pending and result.get("pending"):
                name = str(result.get("candidate_name") or result.get("name") or "").strip()
            if not self._is_known_name(name):
                continue
            box = result.get("box") or {}
            area = int(box.get("w", 0)) * int(box.get("h", 0))
            if area > best_area:
                best_area = area
                best_name = name
        return best_name

    def recognize_identity(
        self,
        read_frame: Any,
        *,
        samples: int | None = None,
        allow_session_hint: bool = True,
        allow_pending: bool = False,
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
            primary = self.primary_viewer(results, allow_pending=allow_pending)
            if primary:
                votes[primary] = votes.get(primary, 0) + 1
            time.sleep(self.identity_sample_gap_s)

        if not saw_face:
            if allow_session_hint:
                hint = self._session_primary_hint()
                if hint:
                    return hint, "recognized"
            return None, "no_face"

        if votes:
            winner = max(votes, key=lambda key: votes[key])
            if votes[winner] >= max(2, (sample_count + 1) // 2):
                return winner, "recognized"

        if allow_session_hint:
            hint = self._session_primary_hint()
            if hint:
                return hint, "recognized"

        return None, "unknown"

    # ------------------------------------------------------------- persistence

    def _save_embeddings_locked(self) -> None:
        payload = {
            "version": 1,
            "model": "sface_2021dec",
            "people": {
                person_id: {
                    "name": display_name,
                    "embeddings": embs.tolist(),
                }
                for person_id, (display_name, embs) in self._embeddings.items()
            },
        }
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.embeddings_path.write_text(json.dumps(payload), encoding="utf-8")

    def _load_embeddings(self) -> None:
        if not self.embeddings_path.is_file():
            return
        try:
            payload = json.loads(self.embeddings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("Corrupt %s — ignoring", self.embeddings_path.name)
            return
        if payload.get("version") != 1 or payload.get("model") != "sface_2021dec":
            return

        store: dict[str, tuple[str, np.ndarray]] = {}
        for person_id, entry in payload.get("people", {}).items():
            embs = np.asarray(entry.get("embeddings", []), dtype=np.float32)
            if embs.ndim != 2 or embs.shape[0] == 0:
                continue
            pruned = self._prune_embeddings(embs)
            store[person_id] = (
                str(entry.get("name") or self._display_name(person_id)),
                pruned,
            )

        with self._lock:
            self._embeddings = store
        if store:
            pruned_total = sum(embs.shape[0] for _, embs in store.values())
            logger.info(
                "Loaded embeddings for %d people (%d after outlier prune)",
                len(store),
                pruned_total,
            )
            # Persist pruned store so ghost-ID outlier samples are removed on disk.
            if pruned_total < sum(
                len(np.asarray(entry.get("embeddings", [])))
                for entry in payload.get("people", {}).values()
            ):
                with self._lock:
                    self._save_embeddings_locked()

    # ------------------------------------------------------------------ models

    def _init_yunet_detector(self) -> None:
        if not hasattr(cv2, "FaceDetectorYN"):
            logger.warning("YuNet unavailable — using Haar cascade for face detection")
            return
        if not self._ensure_model(self._yunet_model_path, YUNET_MODEL_URL):
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
            logger.info("YuNet face detector ready")
        except cv2.error as exc:
            logger.warning("YuNet init failed (%s) — using Haar cascade", exc)

    def _init_sface_recognizer(self) -> None:
        if not hasattr(cv2, "FaceRecognizerSF"):
            logger.error(
                "cv2.FaceRecognizerSF missing — upgrade opencv-python to >= 4.5.4"
            )
            return
        if not self._ensure_model(self._sface_model_path, SFACE_MODEL_URL):
            logger.error("SFace model missing and download failed — recognition disabled")
            return
        try:
            self._sface = cv2.FaceRecognizerSF.create(str(self._sface_model_path), "")
            logger.info("SFace embedding recognizer ready (cosine threshold %.2f)", self.match_threshold)
        except cv2.error as exc:
            logger.error("SFace init failed: %s", exc)

    @staticmethod
    def _ensure_model(path: Path, url: str) -> bool:
        if path.is_file() and path.stat().st_size > 100_000:
            return True
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            logger.info("Downloading %s", url)
            with urllib.request.urlopen(url, timeout=120) as resp:
                data = resp.read()
            path.write_bytes(data)
            return True
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            logger.warning("Could not download %s: %s", path.name, exc)
            return False

    # ------------------------------------------------------------------- names

    def _person_id(self, name: str) -> str:
        # Keep letters from any script (e.g. Devanagari); slug the rest.
        cleaned = unicodedata.normalize("NFKC", (name or "").strip().lower())
        cleaned = re.sub(r"[^\w\-]+", "_", cleaned, flags=re.UNICODE)
        return cleaned.strip("_")

    def same_person(self, name_a: str, name_b: str) -> bool:
        """True when two display names map to the same registered person slug."""
        a = self._person_id(name_a)
        b = self._person_id(name_b)
        return bool(a) and a == b

    def identify_registered_face(self, frame: np.ndarray) -> str | None:
        """Match a frame against the embedding DB without recognition side effects."""
        if frame is None or frame.size == 0 or self._sface is None:
            return None

        detections = self._detect_faces(frame)
        if not detections:
            return None

        box, row = max(detections, key=lambda d: d[0][2] * d[0][3])
        detection_valid, detection_score = self._detection_quality_ok(box, row)
        if not detection_valid or detection_score < self.recognition_min_detection_score:
            return None

        aligned = self._aligned_crop(frame, box, row)
        embedding = self._embed(aligned) if aligned is not None else None
        if embedding is None:
            return None

        best_name, best_score, second_best = self._match_embedding(embedding)
        margin = best_score - max(second_best, -1.0)
        block_margin = self.margin_min * 0.75
        if (
            best_name
            and best_score >= self.match_soft_threshold
            and margin >= block_margin
        ):
            return best_name
        return None

    def validate_registration_name(
        self, frame: np.ndarray | None, proposed_name: str
    ) -> tuple[bool, str | None]:
        """Return (allowed, existing_display_name_if_blocked)."""
        if frame is None or frame.size == 0:
            return True, None

        existing = self.identify_registered_face(frame)
        if not existing:
            return True, None
        if self.same_person(proposed_name, existing):
            return True, None
        return False, existing

    def _display_name(self, person_id: str) -> str:
        return person_id.replace("_", " ").title()
