"""Voice input accepts WAV containers or raw 16 kHz mono PCM."""

from __future__ import annotations

import io
import wave

from wav_resample import (
    VOICE_INPUT_SAMPLE_RATE_HZ,
    is_wav_bytes,
    normalize_voice_input_bytes,
    pcm16_mono_to_wav,
    wav_pcm_duration_seconds,
)


def _make_wav(pcm: bytes, sample_rate: int = VOICE_INPUT_SAMPLE_RATE_HZ) -> bytes:
    bio = io.BytesIO()
    with wave.open(bio, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return bio.getvalue()


def test_is_wav_bytes_detects_riff_wave() -> None:
    pcm = b"\x00\x01" * 1600
    wav = _make_wav(pcm)
    assert is_wav_bytes(wav)
    assert not is_wav_bytes(pcm)


def test_normalize_voice_input_wraps_raw_pcm() -> None:
    pcm = b"\x00\x01" * 800
    out, fmt = normalize_voice_input_bytes(pcm)
    assert fmt == "pcm"
    assert is_wav_bytes(out)
    assert abs(wav_pcm_duration_seconds(out) - 0.05) < 0.01


def test_normalize_voice_input_passes_through_wav() -> None:
    pcm = b"\x00\x01" * 800
    wav = _make_wav(pcm)
    out, fmt = normalize_voice_input_bytes(wav)
    assert fmt == "wav"
    assert out == wav


def test_pcm16_mono_to_wav_rejects_odd_byte_length() -> None:
    try:
        pcm16_mono_to_wav(b"\x00\x01\x02")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
