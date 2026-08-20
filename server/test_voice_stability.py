"""Silent-clip and session-close gates for the voice assistant."""

from __future__ import annotations

import io
import os
import wave

import numpy as np

from voice_service import (
    clip_peak_energy,
    extract_wake_and_command,
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


def test_transcript_without_ok_nino_is_not_rejected() -> None:
    found, command, phrase = extract_wake_and_command("what time is it")
    assert found is False
    assert command == "what time is it"
    assert phrase == ""

    found, command, phrase = extract_wake_and_command("ok nino what time is it")
    assert found is True
    assert command == "what time is it"
    assert "nino" in phrase

    found, command, phrase = extract_wake_and_command("hello")
    assert found is True
    assert command == ""
    assert phrase.startswith("hell")

    found, command, phrase = extract_wake_and_command("Hello, what time is it")
    assert found is True
    assert command == "what time is it"

    found, command, phrase = extract_wake_and_command("please tell john hello later")
    assert found is False
    assert command == "please tell john hello later"


def test_speech_without_wake_phrase_is_answered() -> None:
    os.environ["VOICE_MIN_ENERGY"] = "5"
    tone = (8000 * np.sin(2 * np.pi * 220 * np.arange(16000) / 16000)).astype(np.int16)
    wav = _mono_wav(tone)
    dummy_wav = _mono_wav(np.zeros(1600, dtype=np.int16))

    from unittest.mock import patch

    with (
        patch("voice_service.transcribe_wav", return_value=("what time is it", "mock")),
        patch("voice_service.synthesize_sapi_wav_bytes", return_value=(dummy_wav, "mock")),
        patch("voice_service.resample_wav_bytes_to_mono_16bit", return_value=dummy_wav),
        patch(
            "voice_service.last_tts_synthesis_info",
            return_value={"provider": "mock", "voice": "mock"},
        ),
    ):
        out, meta = process_voice_wav(
            wav,
            session_kind="continue",
            aux_energy=99,
            device_id="test",
            voice_turn=8,
        )

    assert out
    assert meta.timings["reply_path"] != "wake_reject"
    assert meta.timings["reply_path"] == "local_time"


def test_wake_without_phrase_is_rejected() -> None:
    os.environ["VOICE_MIN_ENERGY"] = "5"
    tone = (8000 * np.sin(2 * np.pi * 220 * np.arange(16000) / 16000)).astype(np.int16)
    wav = _mono_wav(tone)

    from unittest.mock import patch

    with patch("voice_service.transcribe_wav", return_value=("what time is it", "mock")):
        out, meta = process_voice_wav(
            wav,
            session_kind="wake",
            aux_energy=99,
            device_id="test",
            voice_turn=1,
        )

    assert meta.timings["reply_path"] == "wake_reject"
    assert meta.timings["wake_ok"] is False
    assert meta.end_session is True
    assert not out or len(out) > 0


def test_wake_with_ok_nino_is_accepted() -> None:
    os.environ["VOICE_MIN_ENERGY"] = "5"
    tone = (8000 * np.sin(2 * np.pi * 220 * np.arange(16000) / 16000)).astype(np.int16)
    wav = _mono_wav(tone)

    from unittest.mock import patch

    with patch(
        "voice_service.transcribe_wav", return_value=("ok nino what time is it", "mock")
    ):
        out, meta = process_voice_wav(
            wav,
            session_kind="wake",
            aux_energy=99,
            device_id="test",
            voice_turn=1,
        )

    assert meta.timings["reply_path"] == "wake_ok"
    assert meta.timings["wake_ok"] is True
    assert meta.end_session is False
    assert out == b""


def test_wake_with_hello_is_accepted() -> None:
    os.environ["VOICE_MIN_ENERGY"] = "5"
    tone = (8000 * np.sin(2 * np.pi * 220 * np.arange(16000) / 16000)).astype(np.int16)
    wav = _mono_wav(tone)

    from unittest.mock import patch

    with patch("voice_service.transcribe_wav", return_value=("hello", "mock")):
        out, meta = process_voice_wav(
            wav,
            session_kind="wake",
            aux_energy=99,
            device_id="test",
            voice_turn=1,
        )

    assert meta.timings["reply_path"] == "wake_ok"
    assert meta.timings["wake_ok"] is True
    assert meta.end_session is False
    assert out == b""


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
