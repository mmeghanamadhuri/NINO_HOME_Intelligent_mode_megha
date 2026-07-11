"""
Canonical emotion class order for this project.

Must match tensorflow.keras ImageDataGenerator.flow_from_directory, which
assigns indices alphabetically by subfolder name under train/ and test/.
"""

# Lowercase folder names — use these for archive/train, archive/test, dataset/
EMOTION_CLASSES = [
    "angry",
    "disgust",
    "fear",
    "happy",
    "neutral",
    "sad",
    "surprise",
]

NUM_EMOTIONS = len(EMOTION_CLASSES)

# index -> class name
CLASS_INDICES = {name: idx for idx, name in enumerate(EMOTION_CLASSES)}

# Backwards-compatible alias used in older scripts
class_labels = EMOTION_CLASSES


def emotion_at_index(index: int) -> str:
    return EMOTION_CLASSES[index]


def index_for_emotion(emotion: str) -> int:
    key = emotion.strip().lower()
    if key not in CLASS_INDICES:
        raise ValueError(f"Unknown emotion '{emotion}'. Expected one of {EMOTION_CLASSES}")
    return CLASS_INDICES[key]


def display_name(emotion: str) -> str:
    """Title case for UI and music folders (e.g. angry -> Angry)."""
    return emotion_at_index(index_for_emotion(emotion)).capitalize()


def music_folder_name(emotion: str) -> str:
    return display_name(emotion)


def verify_class_indices(class_indices: dict) -> None:
    """Raise if Keras generator order does not match EMOTION_CLASSES."""
    expected = {name: idx for idx, name in enumerate(EMOTION_CLASSES)}
    if class_indices != expected:
        raise ValueError(
            "Training folder class order does not match emotion_labels.\n"
            f"  Expected: {expected}\n"
            f"  Got:      {class_indices}\n"
            "Rename train/test subfolders to lowercase emotion names in alphabetical order."
        )
