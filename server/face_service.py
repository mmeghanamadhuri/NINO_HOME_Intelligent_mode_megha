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
        # person_id -> (display_name, embeddings ndarray [n_samples, 128])
        self._embeddings: dict[str, tuple[str, np.ndarray]] = {}
        self._session_primary_name: str | None = None
        self._session_primary_at: float = 0.0
        self._primary_candidate_name: str | None = None
        self._primary_candidate_streak = 0
        self._primary_stable_name: str | None = None
        self._primary_stable_until: float = 0.0

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
        self.match_threshold = float(os.environ.get("FACE_MATCH_THRESHOLD", "0.42"))
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
                f"{max(0.20, self.match_threshold - 0.03):.3f}",
            )
        )
        self.margin_min = float(os.environ.get("FACE_MATCH_MARGIN_MIN", "0.045"))
        self.confirm_frames = max(2, int(os.environ.get("FACE_CONFIRM_FRAMES", "3")))
        self.stable_hold_seconds = float(
            os.environ.get("FACE_STABLE_HOLD_SECONDS", "1.0")
        )

        self.detect_min_size = int(os.environ.get("FACE_DETECT_MIN_SIZE", "24"))
        self.yunet_score_threshold = float(os.environ.get("FACE_YUNET_SCORE", "0.55"))
        self.min_face_area_ratio = float(os.environ.get("FACE_MIN_AREA_RATIO", "0.0010"))
        self.min_face_aspect = float(os.environ.get("FACE_MIN_ASPECT", "0.68"))
        self.max_face_aspect = float(os.environ.get("FACE_MAX_ASPECT", "1.42"))
        self.secondary_face_area_ratio = float(
            os.environ.get("FACE_SECONDARY_AREA_RATIO", "0.40")
        )
        self.max_detections = max(1, int(os.environ.get("FACE_MAX_DETECTIONS", "1")))
        self.session_primary_hold_seconds = float(
            os.environ.get("FACE_SESSION_PRIMARY_HOLD_SECONDS", "90")
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
            "engine": "sface" if self._sface is not None else "unavailable",
            "detector": "yunet" if self._yunet_enabled else "haar",
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
        if not detections:
            detections = [(box, None) for box in self._detect_haar(frame)]

        filtered = self._filter_detections(detections, (h, w))
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
            if float(row[14]) < self.yunet_score_threshold:
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
                scaleFactor=1.08,
                minNeighbors=3,
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

    # --------------------------------------------------------------- embedding

    def _aligned_crop(
        self, frame: np.ndarray, box: tuple[int, int, int, int], row: np.ndarray | None
    ) -> np.ndarray | None:
        """112x112 BGR crop for SFace — landmark-aligned when YuNet gave us a row."""
        bgr = frame if frame.ndim == 3 else cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)

        if row is not None and self._sface is not None:
            try:
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

    def _match_embedding(self, embedding: np.ndarray) -> tuple[str | None, float, float]:
        """Returns (best_name, best_score, second_best_score) across all people."""
        best_name: str | None = None
        best_score = -1.0
        second_best_score = -1.0
        with self._lock:
            for _person_id, (display_name, embs) in self._embeddings.items():
                if embs.size == 0:
                    continue
                score = float(np.max(embs @ embedding))
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
                and self._primary_candidate_streak >= self.confirm_frames
            ):
                self._primary_stable_name = self._primary_candidate_name
                self._primary_stable_until = now + self.stable_hold_seconds
                stabilized = True
            elif (
                self._primary_stable_name
                and candidate_name == self._primary_stable_name
                and candidate_score >= self.match_soft_threshold
                and margin_score >= (self.margin_min * 0.5)
            ):
                # Short hysteresis reduces "unknown" flicker while the same face remains.
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
                self._append_embedding(person_id, self._display_name(person_id), embedding)

        return output_path

    def _append_embedding(self, person_id: str, display_name: str, embedding: np.ndarray) -> None:
        with self._lock:
            _, existing = self._embeddings.get(person_id, (display_name, np.empty((0, embedding.shape[0]), dtype=np.float32)))
            stacked = np.vstack([existing, embedding[None, :]])
            if stacked.shape[0] > self.max_samples_per_person:
                stacked = stacked[-self.max_samples_per_person :]
            self._embeddings[person_id] = (display_name, stacked.astype(np.float32))
            self._save_embeddings_locked()

    # ---------------------------------------------------------------- training

    def train(self) -> dict[str, Any]:
        """Re-encode stored samples into embeddings. Fast — no model training."""
        if self._sface is None:
            raise RuntimeError(
                "SFace recognizer unavailable — could not load "
                f"{SFACE_FILENAME} (check the data/models folder or network)."
            )

        new_store: dict[str, tuple[str, np.ndarray]] = {}
        total = 0
        for person_dir in sorted(self.faces_dir.iterdir()):
            if not person_dir.is_dir():
                continue
            sample_paths = sorted(
                person_dir.glob("*.jpg"), key=lambda p: p.stat().st_mtime, reverse=True
            )[: self.max_samples_per_person]
            vectors: list[np.ndarray] = []
            for sample_path in sample_paths:
                embedding = self._embed_image_file(sample_path)
                if embedding is not None:
                    vectors.append(embedding)
            if not vectors:
                continue
            new_store[person_dir.name] = (
                self._display_name(person_dir.name),
                np.vstack(vectors).astype(np.float32),
            )
            total += len(vectors)

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

            name = "Unknown"
            confidence: float | None = None
            recognized = False
            best_name: str | None = None
            best_score = -1.0
            margin = 0.0

            if self._sface is not None:
                aligned = self._aligned_crop(frame, box, row)
                embedding = self._embed(aligned) if aligned is not None else None
                if embedding is not None:
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
                }
            )

        self._stabilize_primary_face(results)
        for result in results:
            if not result.get("stabilized", False):
                result["recognized"] = False

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
            color = (0, 200, 0) if result["recognized"] else (0, 180, 255)
            label = result["name"]
            if result["confidence"] is not None:
                label = f"{label} ({result['confidence']:.2f})"
            if result.get("pending"):
                label = f"{label} [hold]"

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
            box = result.get("box") or {}
            area = int(box.get("w", 0)) * int(box.get("h", 0))
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
            store[person_id] = (str(entry.get("name") or self._display_name(person_id)), embs)

        with self._lock:
            self._embeddings = store
        if store:
            logger.info("Loaded embeddings for %d people", len(store))

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
        cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "_", name.strip().lower())
        return cleaned.strip("_")

    def _display_name(self, person_id: str) -> str:
        return person_id.replace("_", " ").title()
