"""Resample WAV PCM for ESP32 codec (playback and mic must share one sample rate)."""

from __future__ import annotations

import io
import wave
from typing import Literal

import numpy as np

# Match ES8311 / I2S peer clock used after typical Windows SAPI output (see firmware audio_capture).
ESP_PCM_SAMPLE_RATE_HZ = 22050

# Voice WebSocket mic input from ESP (and raw-PCM clients): 16 kHz mono 16-bit LE.
VOICE_INPUT_SAMPLE_RATE_HZ = 16000


def is_wav_bytes(data: bytes) -> bool:
    """True when `data` looks like a canonical RIFF/WAVE PCM container."""
    return (
        len(data) >= 44
        and data[:4] == b"RIFF"
        and data[8:12] == b"WAVE"
    )


def pcm16_mono_to_wav(pcm: bytes, sample_rate: int = VOICE_INPUT_SAMPLE_RATE_HZ) -> bytes:
    """Wrap raw 16-bit little-endian mono PCM in a PCM format-1 WAV."""
    if not pcm:
        raise ValueError("Empty PCM input.")
    if len(pcm) % 2 != 0:
        raise ValueError("PCM byte length must be a multiple of 2 (16-bit samples).")
    bio = io.BytesIO()
    with wave.open(bio, "wb") as wo:
        wo.setnchannels(1)
        wo.setsampwidth(2)
        wo.setframerate(sample_rate)
        wo.writeframes(pcm)
    return bio.getvalue()


def normalize_voice_input_bytes(
    data: bytes,
    *,
    pcm_sample_rate: int = VOICE_INPUT_SAMPLE_RATE_HZ,
) -> tuple[bytes, Literal["wav", "pcm"]]:
    """Accept WAV or raw 16-bit mono PCM; always return canonical WAV for STT."""
    if not data:
        raise ValueError("Empty audio input.")
    if is_wav_bytes(data):
        return data, "wav"
    return pcm16_mono_to_wav(data, pcm_sample_rate), "pcm"


def wav_pcm_duration_seconds(wav_bytes: bytes) -> float:
    """Duration from a PCM WAV header; 0.0 if the container cannot be read."""
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            rate = wf.getframerate()
            if rate <= 0:
                return 0.0
            return wf.getnframes() / float(rate)
    except Exception:
        return 0.0


def _resample_mono_float(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    if src_rate == dst_rate:
        return samples
    duration = len(samples) / float(src_rate)
    target_n = max(1, int(duration * dst_rate))
    x_old = np.linspace(0.0, 1.0, num=len(samples), endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=target_n, endpoint=False)
    return np.interp(x_new, x_old, samples).astype(np.float32)


def _soften_for_small_speaker(samples: np.ndarray) -> np.ndarray:
    """Trim raspy treble and leave PA headroom for the P4 demo speaker."""
    if samples.size < 3:
        return samples
    kernel = np.array([0.18, 0.64, 0.18], dtype=np.float32)
    return np.convolve(samples, kernel, mode="same") * np.float32(0.88)


def resample_wav_bytes_to_mono_16bit(wav_bytes: bytes, target_sr: int) -> bytes:
    """16-bit PCM WAV in → mono 16-bit PCM WAV at target_sr (linear resample)."""
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        nch = wf.getnchannels()
        sw = wf.getsampwidth()
        sr = wf.getframerate()
        nframes = wf.getnframes()
        if sw != 2:
            raise ValueError("Only 16-bit PCM WAV is supported.")
        raw = wf.readframes(nframes)
    pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if nch == 2:
        pcm = pcm.reshape(-1, 2).mean(axis=1)
    elif nch != 1:
        raise ValueError("Only mono or stereo WAV is supported.")
    # Always write a fresh RIFF/fmt (PCM format tag 1). Windows SAPI may emit
    # WAVE_FORMAT_EXTENSIBLE (0xFFFE); ESP parse_wav_pcm only accepts format 1.
    if sr != target_sr:
        pcm = _resample_mono_float(pcm, sr, target_sr)
    pcm = _soften_for_small_speaker(pcm)
    out_i16 = (np.clip(pcm, -1.0, 1.0) * 32767.0).astype(np.int16)
    bio = io.BytesIO()
    with wave.open(bio, "wb") as wo:
        wo.setnchannels(1)
        wo.setsampwidth(2)
        wo.setframerate(target_sr)
        wo.writeframes(out_i16.tobytes())
    return bio.getvalue()
