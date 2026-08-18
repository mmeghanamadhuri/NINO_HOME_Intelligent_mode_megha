"""Silent-clip and session-close gates for the voice assistant."""

from __future__ import annotations

import io
import os
import wave

import numpy as np

from voice_service import (
    clip_peak_energy,
    min_speech_energy,
    process_voice_wav,
    should_continue_listen_after_reply,
    wav_peak_frame_energy,
)


def _mono_wav(samples: np.ndarray, rate: int = 16000) -> bytes:
    pcm = np.asarray(samples, dtype=np.int16)
    bio = io.BytesIO()
    with wave.open(bio, "wb") as wo:
        wo.setnchannels(1)
        wo.setsampwidth(2)
        wo.setframerate(rate)
        wo.writeframes(pcm.tobytes())
    return bio.getvalue()


def test_silence_peak_energy_is_zero() -> None:
    wav = _mono_wav(np.zeros(16000, dtype=np.int16))
    assert wav_peak_frame_energy(wav) == 0


def test_speech_like_peak_energy() -> None:
    tone = (8000 * np.sin(2 * np.pi * 220 * np.arange(16000) / 16000)).astype(np.int16)
    assert wav_peak_frame_energy(_mono_wav(tone)) >= 1000


def test_clip_peak_uses_reported_when_higher() -> None:
    wav = _mono_wav(np.zeros(1600, dtype=np.int16))
    assert clip_peak_energy(wav, 12) == 12
    assert clip_peak_energy(wav, 0) == 0


def test_empty_and_silent_paths_close_session() -> None:
    assert should_continue_listen_after_reply("stt_empty", "hello") is False
    assert should_continue_listen_after_reply("stt_silent", "hello") is False
    assert should_continue_listen_after_reply("stt_rejected", "hello") is False
    assert should_continue_listen_after_reply("wake_reject", "ok nino") is False


def test_good_reply_can_keep_session_open() -> None:
    os.environ["VOICE_CONTINUE_LISTEN"] = "1"
    assert should_continue_listen_after_reply("llm", "what time is it") is True
    assert should_continue_listen_after_reply("llm", "goodbye") is False


def test_low_energy_skips_stt_and_does_not_reopen() -> None:
    os.environ["VOICE_MIN_ENERGY"] = "5"
    wav = _mono_wav(np.zeros(8000, dtype=np.int16))
    out, meta = process_voice_wav(
        wav,
        session_kind="wake",
        aux_energy=1,
        device_id="test",
        voice_turn=7,
    )
    assert out
    assert meta.prompt_medical_ack is False
    assert meta.timings["reply_path"] == "stt_silent"
    assert meta.timings["stt_engine"] == "skipped"
    assert meta.timings["turn"] == 7
    assert min_speech_energy() == 5
