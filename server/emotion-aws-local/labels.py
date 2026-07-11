"""Emotion label helpers for AWS Rekognition mapping."""

EMOTION_CLASSES = [
    "angry",
    "disgust",
    "fear",
    "happy",
    "neutral",
    "sad",
    "surprise",
]


def display_name(emotion: str) -> str:
    key = emotion.strip().lower()
    if key not in EMOTION_CLASSES:
        return "Uncertain"
    return key.capitalize()
