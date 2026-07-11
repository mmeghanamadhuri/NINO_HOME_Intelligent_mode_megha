"""AWS Rekognition face emotion detection."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO

import boto3
import cv2
import numpy as np
from botocore.exceptions import BotoCoreError, ClientError
from PIL import Image

from aws_config import AwsConfig, get_aws_config
from labels import display_name

AWS_TO_PROJECT_EMOTION: dict[str, str] = {
    "ANGRY": "angry",
    "CALM": "neutral",
    "CONFUSED": "fear",
    "DISGUSTED": "disgust",
    "FEAR": "fear",
    "HAPPY": "happy",
    "SAD": "sad",
    "SURPRISED": "surprise",
    "UNKNOWN": "uncertain",
}

MAX_IMAGE_BYTES = 5 * 1024 * 1024


@dataclass(frozen=True)
class RekognitionEmotionScore:
    aws_type: str
    confidence: float


@dataclass(frozen=True)
class RekognitionFaceResult:
    project_emotion: str
    emotion_display: str
    aws_emotion: str
    confidence: float
    face_box: tuple[int, int, int, int]
    face_confidence: float
    all_emotions: tuple[RekognitionEmotionScore, ...]


class RekognitionEmotionClient:
    def __init__(self, config: AwsConfig | None = None):
        self._config = config or get_aws_config()
        self._client = boto3.client(
            "rekognition",
            region_name=self._config.region,
            aws_access_key_id=self._config.access_key_id,
            aws_secret_access_key=self._config.secret_access_key,
        )

    @property
    def region(self) -> str:
        return self._config.region

    def detect_all_emotions(
        self, image_bytes: bytes, image_size: tuple[int, int]
    ) -> list[RekognitionFaceResult]:
        """Return one result per detected face (largest area first)."""
        if len(image_bytes) > MAX_IMAGE_BYTES:
            raise ValueError(
                f"Image is too large for Rekognition ({len(image_bytes) // 1024} KB). "
                "Use a photo under 5 MB."
            )

        try:
            response = self._client.detect_faces(
                Image={"Bytes": image_bytes},
                Attributes=["ALL"],
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "ClientError")
            message = exc.response.get("Error", {}).get("Message", str(exc))
            raise RuntimeError(f"AWS Rekognition error ({code}): {message}") from exc
        except BotoCoreError as exc:
            raise RuntimeError(f"AWS connection error: {exc}") from exc

        img_w, img_h = image_size
        parsed: list[RekognitionFaceResult] = []
        for face in response.get("FaceDetails") or []:
            item = self._parse_face(face, img_w, img_h)
            if item is not None:
                parsed.append(item)

        parsed.sort(key=lambda r: r.face_box[2] * r.face_box[3], reverse=True)
        return parsed

    def detect_emotions(self, image_bytes: bytes, image_size: tuple[int, int]) -> RekognitionFaceResult | None:
        faces = self.detect_all_emotions(image_bytes, image_size)
        return faces[0] if faces else None

    def _parse_face(self, face: dict, img_w: int, img_h: int) -> RekognitionFaceResult | None:
        bbox = face.get("BoundingBox") or {}
        if not bbox:
            return None

        left = float(bbox.get("Left", 0.0))
        top = float(bbox.get("Top", 0.0))
        width = float(bbox.get("Width", 0.0))
        height = float(bbox.get("Height", 0.0))

        x = max(0, int(left * img_w))
        y = max(0, int(top * img_h))
        w = max(1, int(width * img_w))
        h = max(1, int(height * img_h))
        w = min(w, img_w - x)
        h = min(h, img_h - y)

        emotions = face.get("Emotions") or []
        if not emotions:
            return None

        sorted_emotions = sorted(
            emotions,
            key=lambda item: float(item.get("Confidence", 0.0)),
            reverse=True,
        )
        top_emotion = sorted_emotions[0]
        aws_type = str(top_emotion.get("Type", "UNKNOWN")).upper()
        confidence = float(top_emotion.get("Confidence", 0.0)) / 100.0

        project_emotion = AWS_TO_PROJECT_EMOTION.get(aws_type, "uncertain")
        emotion_display = (
            "Uncertain"
            if project_emotion == "uncertain"
            else display_name(project_emotion)
        )

        all_emotions = tuple(
            RekognitionEmotionScore(
                aws_type=str(item.get("Type", "UNKNOWN")).upper(),
                confidence=float(item.get("Confidence", 0.0)),
            )
            for item in sorted_emotions
        )

        return RekognitionFaceResult(
            project_emotion=project_emotion,
            emotion_display=emotion_display,
            aws_emotion=aws_type,
            confidence=confidence,
            face_box=(x, y, w, h),
            face_confidence=float(face.get("Confidence", 0.0)) / 100.0,
            all_emotions=all_emotions,
        )


def prepare_jpeg_bytes(bgr: np.ndarray, quality: int = 85) -> bytes:
    for q in (quality, 75, 65, 55):
        ok, buffer = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), q])
        if ok and len(buffer.tobytes()) <= MAX_IMAGE_BYTES:
            return buffer.tobytes()

    scale = 0.85
    while scale >= 0.35:
        resized = cv2.resize(
            bgr,
            (max(1, int(bgr.shape[1] * scale)), max(1, int(bgr.shape[0] * scale))),
            interpolation=cv2.INTER_AREA,
        )
        ok, buffer = cv2.imencode(".jpg", resized, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
        if ok and len(buffer.tobytes()) <= MAX_IMAGE_BYTES:
            return buffer.tobytes()
        scale -= 0.15

    ok, buffer = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 55])
    return buffer.tobytes()


def decode_upload_bytes(raw: bytes) -> np.ndarray | None:
    if not raw:
        return None
    image = Image.open(BytesIO(raw)).convert("RGB")
    rgb = np.array(image)
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
