"""Load the 48×48 grayscale FER CNN (weights-only .h5)."""

from __future__ import annotations

from tensorflow.keras.layers import Conv2D, Dense, Dropout, Flatten, MaxPooling2D
from tensorflow.keras.models import Sequential


def build_emotion_model(num_classes: int = 7) -> Sequential:
    return Sequential(
        [
            Conv2D(32, (3, 3), activation="relu", input_shape=(48, 48, 1)),
            MaxPooling2D(2, 2),
            Conv2D(64, (3, 3), activation="relu"),
            MaxPooling2D(2, 2),
            Conv2D(128, (3, 3), activation="relu"),
            MaxPooling2D(2, 2),
            Flatten(),
            Dense(256, activation="relu"),
            Dropout(0.5),
            Dense(num_classes, activation="softmax"),
        ]
    )


def load_emotion_model(path: str, num_classes: int = 7) -> Sequential:
    model = build_emotion_model(num_classes)
    model.load_weights(path)
    return model
