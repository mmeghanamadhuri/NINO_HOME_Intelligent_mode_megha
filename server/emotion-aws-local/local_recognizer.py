"""DrGM ConvNeXt V2 local emotion inference on a BGR face crop."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

logger = logging.getLogger(__name__)

MODEL_DIR = Path(__file__).resolve().parent / "drgm_convnextv2l_fer7"


@dataclass(frozen=True)
class LocalEmotionResult:
    emotion: str
    confidence: float


class LocalEmotionRecognizer:
    """Load DrGM weights once; predict on padded face crops."""

    def __init__(self) -> None:
        self._model = None
        self._processor = None
        self._device = "cpu"
        self._ready = False

    @property
    def available(self) -> bool:
        return self._ready

    def load(self) -> None:
        if self._ready:
            return
        if not (MODEL_DIR / "config.json").is_file():
            raise FileNotFoundError(f"Local emotion model not found at {MODEL_DIR}")
        if not (MODEL_DIR / "model.safetensors").is_file():
            raise FileNotFoundError(f"model.safetensors missing under {MODEL_DIR}")

        import torch
        from transformers import AutoImageProcessor, AutoModelForImageClassification

        logger.info("Loading local emotion model from %s", MODEL_DIR.name)
        self._processor = AutoImageProcessor.from_pretrained(str(MODEL_DIR))
        self._model = AutoModelForImageClassification.from_pretrained(str(MODEL_DIR))
        self._model.eval()
        self._device = "cuda" if torch.cuda.is_available() else "cpu"
        self._model.to(self._device)
        self._ready = True
        logger.info("Local emotion model ready (device=%s)", self._device)

    def predict_crop(self, face_bgr: np.ndarray) -> LocalEmotionResult | None:
        if not self._ready or face_bgr is None or face_bgr.size == 0:
            return None

        import torch
        from PIL import Image

        rgb = cv2.cvtColor(face_bgr, cv2.COLOR_BGR2RGB)
        pil = Image.fromarray(rgb)
        inputs = self._processor(images=pil, return_tensors="pt")
        inputs = {key: tensor.to(self._device) for key, tensor in inputs.items()}

        with torch.no_grad():
            logits = self._model(**inputs).logits
            probs = torch.softmax(logits, dim=-1)[0]
            idx = int(probs.argmax())
            confidence = float(probs[idx])

        id2label = self._model.config.id2label
        raw = id2label.get(idx, id2label.get(str(idx), "neutral"))
        emotion = str(raw).strip().lower()
        return LocalEmotionResult(emotion=emotion, confidence=confidence)

    @staticmethod
    def square_padded_crop(
        frame_bgr: np.ndarray,
        box: dict[str, int],
        *,
        pad_ratio: float = 0.2,
    ) -> np.ndarray | None:
        x, y, w, h = int(box["x"]), int(box["y"]), int(box["w"]), int(box["h"])
        fh, fw = frame_bgr.shape[:2]
        if w < 8 or h < 8:
            return None

        side = int(max(w, h) * (1.0 + pad_ratio * 2.0))
        cx, cy = x + w // 2, y + h // 2
        x1 = max(0, cx - side // 2)
        y1 = max(0, cy - side // 2)
        x2 = min(fw, x1 + side)
        y2 = min(fh, y1 + side)
        x1 = max(0, x2 - side)
        y1 = max(0, y2 - side)
        crop = frame_bgr[y1:y2, x1:x2]
        return crop if crop.size > 0 else None
