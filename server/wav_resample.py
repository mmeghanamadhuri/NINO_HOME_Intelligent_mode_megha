"""Resample WAV PCM for ESP32 codec (playback and mic must share one sample rate)."""

from __future__ import annotations

import io
import wave

import numpy as np

# Match ES8311 / I2S peer clock used after typical Windows SAPI output (see firmware audio_capture).
ESP_PCM_SAMPLE_RATE_HZ = 22050


def _resample_mono_float(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    if src_rate == dst_rate:
        return samples
    duration = len(samples) / float(src_rate)
    target_n = max(1, int(duration * dst_rate))
    x_old = np.linspace(0.0, 1.0, num=len(samples), endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=target_n, endpoint=False)
    return np.interp(x_new, x_old, samples).astype(np.float32)


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
    out_i16 = (np.clip(pcm, -1.0, 1.0) * 32767.0).astype(np.int16)
    bio = io.BytesIO()
    with wave.open(bio, "wb") as wo:
        wo.setnchannels(1)
        wo.setsampwidth(2)
        wo.setframerate(target_sr)
        wo.writeframes(out_i16.tobytes())
    return bio.getvalue()
