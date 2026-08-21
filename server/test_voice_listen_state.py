"""Post-TTS grace and continue-listen preservation."""

from __future__ import annotations

import io
import os
import time
import wave

import numpy as np

import voice_listen_state as vls
from voice_service import process_voice_wav


def _mono_wav(samples: np.ndarray, rate: int = 16000) -> bytes:
    pcm = np.asarray(samples, dtype=np.int16)
    bio = io.BytesIO()
    with wave.open(bio, "wb") as wo:
        wo.setnchannels(1)
        wo.setsampwidth(2)
        wo.setframerate(rate)
        wo.writeframes(pcm.tobytes())
    return bio.getvalue()


def test_post_tts_grace_skips_long_low_mean_rejection() -> None:
    os.environ["VOICE_MIN_ENERGY"] = "5"
    os.environ["VOICE_LONG_CLIP_MIN_MEAN_ENERGY"] = "18"
    vls.mark_session_open("sess-grace", "dev-grace")
    vls.mark_tts_playback("sess-grace", "dev-grace", audio_out_seconds=10.0)
    assert vls.in_post_tts_grace("sess-grace", "dev-grace")

    wav = _mono_wav(np.zeros(8 * 16000, dtype=np.int16))

    from unittest.mock import patch

    with (
        patch("voice_service.transcribe_wav", return_value=("hello there", "mock")),
        patch("voice_service.wav_mean_frame_energy", return_value=15),
        patch("voice_service.wav_peak_frame_energy", return_value=30),
    ):
        out, meta = process_voice_wav(
            wav,
            session_kind="continue",
            aux_energy=99,
            device_id="dev-grace",
            session_id="sess-grace",
            voice_turn=3,
        )
    assert meta.timings["reply_path"] != "stt_silent"
    assert meta.timings["stt_engine"] == "mock"
    assert out


def test_thank_you_preserves_continue_listen() -> None:
    os.environ["VOICE_MIN_ENERGY"] = "5"
    vls.mark_session_open("sess-thx", "dev-thx")
    tone = (8000 * np.sin(2 * np.pi * 220 * np.arange(16000) / 16000)).astype(np.int16)
    wav = _mono_wav(tone)

    from unittest.mock import patch

    with patch("voice_service.transcribe_wav", return_value=("Thank you.", "mock")):
        out, meta = process_voice_wav(
            wav,
            session_kind="continue",
            aux_energy=99,
            device_id="dev-thx",
            session_id="sess-thx",
            voice_turn=4,
        )

    assert meta.timings["reply_path"] == "stt_rejected"
    assert meta.prompt_medical_ack is True
    assert meta.timings["continue_listen"] is True
    assert out


def test_grace_expires() -> None:
    vls.mark_session_closed("sess-exp", "dev-exp")
    vls.mark_session_open("sess-exp", "dev-exp")
    os.environ["VOICE_POST_TTS_GRACE_SECONDS"] = "0.05"
    os.environ["VOICE_POST_TTS_GRACE_TTS_FACTOR"] = "0"
    vls.mark_tts_playback("sess-exp", "dev-exp", audio_out_seconds=1.0)
    assert vls.in_post_tts_grace("sess-exp", "dev-exp")
    time.sleep(0.08)
    assert not vls.in_post_tts_grace("sess-exp", "dev-exp")


def test_peak_override_allows_long_low_mean_clip() -> None:
    from voice_service import speech_like_clip

    os.environ["VOICE_LONG_CLIP_PEAK_OVERRIDE"] = "80"
    assert speech_like_clip(292, 14)
    assert not speech_like_clip(40, 14)
