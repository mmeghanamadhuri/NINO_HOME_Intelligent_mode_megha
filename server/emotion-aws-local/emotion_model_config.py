"""Single configuration point for the DrGM emotion model."""

from __future__ import annotations

from pathlib import Path
from typing import Any, TypedDict

MODELS_DIR = Path(__file__).resolve().parent
ACTIVE_EMOTION_MODEL = "drgm_convnextv2l_fer7"

IMAGENET_MEAN = [0.485, 0.456, 0.406]
IMAGENET_STD = [0.229, 0.224, 0.225]


class EmotionModelSpec(TypedDict):
    architecture: str
    num_classes: int
    input_size: int
    onnx_path: Path
    pytorch_checkpoint: Path | None
    huggingface_repo: str | None
    huggingface_local_dir: Path | None
    prefer_onnx: bool
    image_mean: list[float]
    image_std: list[float]
    use_expressive_refinement: bool


EMOTION_MODEL_REGISTRY: dict[str, EmotionModelSpec] = {
    "drgm_convnextv2l_fer7": {
        "architecture": "huggingface_fer7",
        "num_classes": 7,
        "input_size": 224,
        "onnx_path": MODELS_DIR / "drgm_convnextv2l_fer7.onnx",
        "pytorch_checkpoint": None,
        "huggingface_repo": "DrGM/DrGM-ConvNeXt-V2L-Facial-Emotion-Recognition",
        "huggingface_local_dir": MODELS_DIR / "drgm_convnextv2l_fer7",
        "prefer_onnx": False,
        "image_mean": IMAGENET_MEAN,
        "image_std": IMAGENET_STD,
        "use_expressive_refinement": False,
    },
}


def get_active_model_spec() -> EmotionModelSpec:
    if ACTIVE_EMOTION_MODEL not in EMOTION_MODEL_REGISTRY:
        known = ", ".join(sorted(EMOTION_MODEL_REGISTRY))
        raise ValueError(
            f"Unknown ACTIVE_EMOTION_MODEL={ACTIVE_EMOTION_MODEL!r}. "
            f"Choose one of: {known}"
        )
    return EMOTION_MODEL_REGISTRY[ACTIVE_EMOTION_MODEL]


def resolve_model_paths(spec: EmotionModelSpec | None = None) -> dict[str, Any]:
    spec = spec or get_active_model_spec()
    onnx_path = Path(spec["onnx_path"])
    onnx_data = onnx_path.with_suffix(onnx_path.suffix + ".data")
    pytorch_path = spec.get("pytorch_checkpoint")
    local_hf = spec.get("huggingface_local_dir")
    local_hf_path = Path(local_hf) if local_hf else None
    local_hf_available = bool(
        local_hf_path
        and (local_hf_path / "config.json").is_file()
        and (
            (local_hf_path / "model.safetensors").is_file()
            or (local_hf_path / "pytorch_model.bin").is_file()
        )
    )
    return {
        "spec": spec,
        "onnx_path": onnx_path,
        "onnx_data_path": onnx_data,
        "onnx_available": onnx_path.exists(),
        "pytorch_path": Path(pytorch_path) if pytorch_path else None,
        "pytorch_available": bool(pytorch_path and Path(pytorch_path).exists()),
        "huggingface_repo": spec.get("huggingface_repo"),
        "huggingface_local_dir": local_hf_path,
        "huggingface_local_available": local_hf_available,
        "prefer_onnx": spec["prefer_onnx"],
    }
