"""Camera emotion detection on face bounding boxes (Keras CNN default, FER+ ONNX optional)."""

from __future__ import annotations

import logging
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import cv2
import numpy as np

logger = logging.getLogger(__name__)

EMOTION_KERAS_FILENAME = "emotion_model_best.h5"
EMOTION_FERPLUS_FILENAME = "emotion-ferplus-8.onnx"
EMOTION_FERPLUS_URL = (
    "https://github.com/onnx/models/raw/main/"
    "validated/vision/body_analysis/emotion_ferplus/model/"
    f"{EMOTION_FERPLUS_FILENAME}"
)

FERPLUS_LABELS: tuple[str, ...] = (
    "neutral",
    "happy",
    "surprise",
    "sad",
    "anger",
    "disgust",
    "fear",
    "contempt",
)

EMOTION_TO_EYE: dict[str, str | None] = {
    "neutral": None,
    "happy": "happy",
    "surprise": "surprised",
    "sad": "sad",
    "anger": "sad",
    "disgust": None,
    "fear": "surprised",
    "contempt": None,
}

EMOTION_SPOKEN: dict[str, str] = {
    "neutral": "neutral",
    "happy": "happy",
    "surprise": "surprised",
    "sad": "sad",
    "anger": "upset or angry",
    "disgust": "disgusted",
    "fear": "worried or fearful",
    "contempt": "contemptuous",
}

SPEAKABLE_EMOTIONS: frozenset[str] = frozenset({"happy", "surprise", "sad", "anger", "fear"})

EMOTION_INPUT_SIZE = (64, 64)
KERAS_FER_LABELS: tuple[str, ...] = (
    "anger",
    "disgust",
    "fear",
    "happy",
    "sad",
    "surprise",
    "neutral",
)
KERAS_INPUT_SIZE = (48, 48)
KERAS_SEED_MODEL = (
    Path(__file__).resolve().parent.parent / "emotion-trial" / EMOTION_KERAS_FILENAME
)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return float(default)
    return float(raw)


def _softmax(logits: np.ndarray) -> np.ndarray:
    exp = np.exp(logits - np.max(logits))
    return exp / float(np.sum(exp))


def pick_effective_emotion(scores: dict[str, float]) -> tuple[str, float]:
    """Promote a speakable class when neutral dominates but another emotion is strong enough."""
    if not scores:
        return "neutral", 0.0

    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    top_label, top_conf = ranked[0]

    if top_label != "neutral" and top_label in SPEAKABLE_EMOTIONS:
        return top_label, top_conf

    neutral_conf = scores.get("neutral", top_conf if top_label == "neutral" else 0.0)
    min_abs = _env_float("EMOTION_SPEAKABLE_MIN", 0.12)
    min_ratio = _env_float("EMOTION_NEUTRAL_SUPPRESS_RATIO", 0.22)

    best_label = "neutral"
    best_conf = neutral_conf
    for label in SPEAKABLE_EMOTIONS:
        conf = scores.get(label, 0.0)
        if conf < min_abs:
            continue
        if neutral_conf > 0.0 and conf < neutral_conf * min_ratio:
            continue
        if conf > best_conf or (label != "neutral" and best_label == "neutral"):
            best_label = label
            best_conf = conf

    if best_label in SPEAKABLE_EMOTIONS:
        return best_label, best_conf
    return top_label, top_conf


class EmotionService:
    """Run emotion CNN on a preprocessed grayscale face crop."""

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._backend = os.environ.get("EMOTION_BACKEND", "keras").strip().lower()
        self._model_path = data_dir / "models" / EMOTION_KERAS_FILENAME
        self._session: Any | None = None
        self._input_name = "Input3"
        self._provider = "unavailable"
        self._labels: tuple[str, ...] = FERPLUS_LABELS
        self._input_size = EMOTION_INPUT_SIZE
        self._use_clahe = True
        self._min_confidence = _env_float("EMOTION_MIN_CONFIDENCE", 0.12)
        self._init_model()

    @property
    def available(self) -> bool:
        return self._session is not None

    def apply_settings_from_environ(self) -> None:
        self._min_confidence = _env_float("EMOTION_MIN_CONFIDENCE", 0.12)

    def stats(self) -> dict[str, Any]:
        model_name = ""
        if self.available:
            model_name = self._model_path.name
        return {
            "available": self.available,
            "backend": self._backend,
            "model": model_name,
            "provider": self._provider,
            "min_confidence": self._min_confidence,
            "input_size": list(self._input_size),
            "labels": list(self._labels),
        }

    @staticmethod
    def _square_padded_crop(
        frame_bgr: np.ndarray,
        box: dict[str, int] | tuple[int, int, int, int],
        *,
        pad_ratio: float = 0.25,
    ) -> np.ndarray | None:
        if isinstance(box, dict):
            x, y, w, h = int(box["x"]), int(box["y"]), int(box["w"]), int(box["h"])
        else:
            x, y, w, h = (int(v) for v in box)

        fh, fw = frame_bgr.shape[:2]
        if w < 8 or h < 8:
            return None

        side = int(max(w, h) * (1.0 + pad_ratio * 2.0))
        cx = x + w // 2
        cy = y + h // 2
        x1 = max(0, cx - side // 2)
        y1 = max(0, cy - side // 2)
        x2 = min(fw, x1 + side)
        y2 = min(fh, y1 + side)
        x1 = max(0, x2 - side)
        y1 = max(0, y2 - side)
        crop = frame_bgr[y1:y2, x1:x2]
        return crop if crop.size > 0 else None

    def _preprocess_crop(self, crop_bgr: np.ndarray) -> np.ndarray:
        gray = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY)
        if self._use_clahe:
            clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
            gray = clahe.apply(gray)
        resized = cv2.resize(gray, self._input_size, interpolation=cv2.INTER_AREA)
        return resized.astype(np.float32) / 255.0

    def _infer_scores(self, crop_bgr: np.ndarray) -> dict[str, float] | None:
        if self._session is None:
            return None
        if self._backend == "keras":
            return self._infer_scores_keras(crop_bgr)
        return self._infer_scores_ferplus(crop_bgr)

    def _infer_scores_ferplus(self, crop_bgr: np.ndarray) -> dict[str, float] | None:
        tensor = self._preprocess_crop(crop_bgr).reshape(1, 1, *self._input_size)
        try:
            logits = np.asarray(
                self._session.run(None, {self._input_name: tensor})[0],
                dtype=np.float32,
            ).reshape(-1)
        except Exception:
            logger.exception("Emotion inference failed")
            return None

        if logits.size != len(self._labels):
            return None

        probs = _softmax(logits)
        return {label: float(probs[idx]) for idx, label in enumerate(self._labels)}

    def _infer_scores_keras(self, crop_bgr: np.ndarray) -> dict[str, float] | None:
        tensor = self._preprocess_crop(crop_bgr).reshape(1, *self._input_size, 1)
        try:
            probs = np.asarray(self._session.predict(tensor, verbose=0), dtype=np.float32).reshape(-1)
        except Exception:
            logger.exception("Keras emotion inference failed")
            return None

        if probs.size != len(self._labels):
            return None

        return {label: float(probs[idx]) for idx, label in enumerate(self._labels)}

    def detect(
        self,
        frame_bgr: np.ndarray,
        box: dict[str, int] | tuple[int, int, int, int],
    ) -> dict[str, Any] | None:
        crop = self._square_padded_crop(frame_bgr, box)
        if crop is None:
            return None

        scores = self._infer_scores(crop)
        if not scores:
            return None

        raw_label = max(scores, key=scores.get)
        raw_conf = scores[raw_label]
        label, confidence = pick_effective_emotion(scores)

        top3 = sorted(scores.items(), key=lambda item: item[1], reverse=True)[:3]
        result = {
            "label": label,
            "confidence": round(confidence, 3),
            "raw_label": raw_label,
            "raw_confidence": round(raw_conf, 3),
            "scores": {k: round(v, 3) for k, v in top3},
            "eye_expression": EMOTION_TO_EYE.get(label),
            "spoken": EMOTION_SPOKEN.get(label, label),
            "speakable": label in SPEAKABLE_EMOTIONS,
        }

        if label in SPEAKABLE_EMOTIONS and confidence < self._min_confidence:
            return None
        if label not in SPEAKABLE_EMOTIONS and raw_conf < self._min_confidence:
            # Still return neutral/low for overlay when raw signal exists.
            if raw_conf < self._min_confidence:
                return None

        return result

    def detect_overlay(
        self,
        frame_bgr: np.ndarray,
        box: dict[str, int] | tuple[int, int, int, int],
    ) -> dict[str, Any] | None:
        """Always return best-guess labels for MJPEG overlay (no confidence gate)."""
        crop = self._square_padded_crop(frame_bgr, box)
        if crop is None:
            return None
        scores = self._infer_scores(crop)
        if not scores:
            return None

        raw_label = max(scores, key=scores.get)
        label, confidence = pick_effective_emotion(scores)
        top3 = sorted(scores.items(), key=lambda item: item[1], reverse=True)[:3]
        return {
            "label": label,
            "confidence": round(confidence, 3),
            "raw_label": raw_label,
            "raw_confidence": round(scores[raw_label], 3),
            "scores": {k: round(v, 3) for k, v in top3},
            "speakable": label in SPEAKABLE_EMOTIONS,
        }

    def _init_model(self) -> None:
        if self._backend in {"keras", "trial", "h5"}:
            self._init_keras_model()
            return
        self._init_ferplus_model()

    def _init_keras_model(self) -> None:
        raw_path = os.environ.get("EMOTION_KERAS_MODEL_PATH", "").strip()
        self._model_path = Path(raw_path) if raw_path else self._data_dir / "models" / EMOTION_KERAS_FILENAME
        if not self._model_path.is_file() and not raw_path:
            self._seed_keras_model(self._model_path)
        if not self._model_path.is_file():
            logger.warning(
                "Keras emotion model not found at %s — vision emotion pipeline disabled",
                self._model_path,
            )
            return

        self._backend = "keras"
        self._labels = KERAS_FER_LABELS
        self._input_size = KERAS_INPUT_SIZE
        self._use_clahe = False

        try:
            from emotion_model_loader import load_emotion_model

            self._session = load_emotion_model(str(self._model_path), num_classes=len(self._labels))
            self._provider = "tensorflow-cpu"
            logger.info("Emotion Keras ready (%s)", self._model_path.name)
        except ImportError:
            logger.warning(
                "tensorflow not installed — pip install tensorflow or set EMOTION_BACKEND=ferplus"
            )
            self._session = None
        except Exception as exc:
            logger.error("Keras emotion init failed: %s", exc)
            self._session = None

    @staticmethod
    def _seed_keras_model(dest: Path) -> None:
        if not KERAS_SEED_MODEL.is_file():
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            dest.write_bytes(KERAS_SEED_MODEL.read_bytes())
            logger.info("Seeded Keras emotion model at %s", dest)
        except OSError as exc:
            logger.warning("Could not seed Keras model to %s: %s", dest, exc)

    def _init_ferplus_model(self) -> None:
        self._backend = "ferplus"
        self._labels = FERPLUS_LABELS
        self._input_size = EMOTION_INPUT_SIZE
        self._use_clahe = True
        self._model_path = self._data_dir / "models" / EMOTION_FERPLUS_FILENAME
        if not self._ensure_model(self._model_path, EMOTION_FERPLUS_URL):
            logger.warning("Emotion FER+ model unavailable — vision emotion pipeline disabled")
            return

        prefer_gpu = os.environ.get("EMOTION_DEVICE", "auto").strip().lower()
        providers: list[str | tuple[str, dict[str, Any]]] = []
        try:
            import onnxruntime as ort

            available = ort.get_available_providers()
            want_cuda = prefer_gpu in {"auto", "cuda", "gpu"}
            if want_cuda and "CUDAExecutionProvider" in available:
                providers.append(
                    (
                        "CUDAExecutionProvider",
                        {
                            "device_id": int(os.environ.get("EMOTION_CUDA_DEVICE", "0")),
                        },
                    )
                )
            providers.append("CPUExecutionProvider")
            self._session = ort.InferenceSession(
                str(self._model_path),
                providers=providers,
            )
            self._input_name = self._session.get_inputs()[0].name
            active = self._session.get_providers()
            self._provider = active[0] if active else "CPUExecutionProvider"
            logger.info(
                "Emotion FER+ ready (%s) provider=%s",
                self._model_path.name,
                self._provider,
            )
        except ImportError:
            logger.warning("onnxruntime not installed — trying OpenCV DNN fallback")
            self._init_opencv_fallback()
        except Exception as exc:
            logger.error("Emotion ORT init failed: %s — trying OpenCV DNN fallback", exc)
            self._init_opencv_fallback()

    def _init_opencv_fallback(self) -> None:
        try:
            net = cv2.dnn.readNetFromONNX(str(self._model_path))
            if os.environ.get("EMOTION_DEVICE", "auto").strip().lower() in {
                "auto",
                "cuda",
                "gpu",
            }:
                if cv2.cuda.getCudaEnabledDeviceCount() > 0:
                    net.setPreferableBackend(cv2.dnn.DNN_BACKEND_CUDA)
                    net.setPreferableTarget(cv2.dnn.DNN_TARGET_CUDA)
                    self._provider = "opencv-cuda"
                else:
                    self._provider = "opencv-cpu"
            else:
                self._provider = "opencv-cpu"

            class _CvSession:
                def __init__(self, dnn_net: cv2.dnn_Net) -> None:
                    self.net = dnn_net

                def run(self, _out_names: Any, feeds: dict[str, Any]) -> list[np.ndarray]:
                    blob = feeds["Input3"]
                    if blob.ndim == 4:
                        img = (blob[0, 0] * 255.0).astype(np.uint8)
                        blob = cv2.dnn.blobFromImage(
                            img, 1.0 / 255.0, EMOTION_INPUT_SIZE, 0, swapRB=False, crop=False
                        )
                    self.net.setInput(blob)
                    return [self.net.forward()]

                def get_inputs(self) -> list[Any]:
                    class _In:
                        name = "Input3"

                    return [_In()]

                def get_providers(self) -> list[str]:
                    return [self._provider]

            wrapper = _CvSession(net)
            wrapper._provider = self._provider  # type: ignore[attr-defined]
            self._session = wrapper
            self._input_name = "Input3"
            logger.info("Emotion FER+ OpenCV fallback provider=%s", self._provider)
        except cv2.error as exc:
            logger.error("Emotion FER+ init failed: %s", exc)
            self._session = None

    @staticmethod
    def _ensure_model(path: Path, url: str) -> bool:
        if path.is_file() and path.stat().st_size > 100_000:
            return True
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            logger.info("Downloading emotion model %s", url)
            with urllib.request.urlopen(url, timeout=180) as resp:
                data = resp.read()
            path.write_bytes(data)
            return True
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            logger.warning("Could not download %s: %s", path.name, exc)
            return False
