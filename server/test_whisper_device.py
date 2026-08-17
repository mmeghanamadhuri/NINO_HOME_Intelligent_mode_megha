"""GPU vs CPU device selection for local faster-whisper."""

from __future__ import annotations

from voice_service import resolve_whisper_compute_type, resolve_whisper_device


def test_auto_picks_cuda_when_available() -> None:
    assert resolve_whisper_device("auto", cuda_available=True) == "cuda"
    assert resolve_whisper_device("gpu", cuda_available=True) == "cuda"
    assert resolve_whisper_device("cuda", cuda_available=True) == "cuda"


def test_auto_and_cuda_fall_back_to_cpu_without_gpu() -> None:
    assert resolve_whisper_device("auto", cuda_available=False) == "cpu"
    assert resolve_whisper_device("cuda", cuda_available=False) == "cpu"
    assert resolve_whisper_device("cpu", cuda_available=True) == "cpu"


def test_compute_type_defaults_by_device() -> None:
    assert resolve_whisper_compute_type("cuda", "auto") == "float16"
    assert resolve_whisper_compute_type("cpu", "auto") == "int8"
    assert resolve_whisper_compute_type("cuda", "int8_float16") == "int8_float16"
