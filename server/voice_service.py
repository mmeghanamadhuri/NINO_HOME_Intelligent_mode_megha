"""STT (Whisper) + LLM (Ollama) + WAV TTS for /ws/voice and helpers."""

from __future__ import annotations

import io
import os
import wave
import logging
from dataclasses import dataclass
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

from llm_service import answer_voice_query, DEFAULT_MODEL, DEFAULT_OLLAMA_URL
from tts_service import synthesize_sapi_wav_bytes
from wav_resample import resample_wav_bytes_to_mono_16bit

# Voice assistant path uses 16 kHz on device (ESP-SR WakeNet + VAD); face TTS stays 22050 in tts_service.
VOICE_ASSIST_PLAYBACK_HZ = 16000


@dataclass
class VoiceSettings:
    ollama_url: str = DEFAULT_OLLAMA_URL
    ollama_model: str = DEFAULT_MODEL
    whisper_model: str = "tiny"
    whisper_language: str | None = "en"
    max_request_bytes: int = 512_000
    max_words_reply: int = 55


SETTINGS = VoiceSettings()
_WHISPER_MODEL: Any = None


def minimal_voice_reply_wav() -> bytes:
    """Short silent 16-bit mono WAV at VOICE_ASSIST_PLAYBACK_HZ — ESP parse always succeeds."""
    sr = VOICE_ASSIST_PLAYBACK_HZ
    ms = 120
    n = max(1, sr * ms // 1000)
    silence = b"\x00\x00" * n
    bio = io.BytesIO()
    with wave.open(bio, "wb") as wo:
        wo.setnchannels(1)
        wo.setsampwidth(2)
        wo.setframerate(sr)
        wo.writeframes(silence)
    return bio.getvalue()


def configure_from_environ() -> None:
    SETTINGS.ollama_url = os.environ.get("OLLAMA_URL", DEFAULT_OLLAMA_URL).strip()
    SETTINGS.ollama_model = os.environ.get("OLLAMA_MODEL", DEFAULT_MODEL).strip()
    SETTINGS.whisper_model = os.environ.get("WHISPER_MODEL", "tiny").strip()
    lang = os.environ.get("WHISPER_LANGUAGE", "en").strip()
    SETTINGS.whisper_language = None if lang.lower() in {"", "auto"} else lang


def _ensure_whisper() -> Any:
    global _WHISPER_MODEL
    if _WHISPER_MODEL is None:
        from faster_whisper import WhisperModel

        _WHISPER_MODEL = WhisperModel(
            SETTINGS.whisper_model,
            device="cpu",
            compute_type="int8",
        )
    return _WHISPER_MODEL


def _wav_bytes_to_float_mono(wav_bytes: bytes) -> np.ndarray:
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        nch = wf.getnchannels()
        sw = wf.getsampwidth()
        sr = wf.getframerate()
        nframes = wf.getnframes()
        if nframes <= 0:
            raise RuntimeError("Input WAV has no audio frames.")
        raw = wf.readframes(nframes)
    if sw != 2:
        raise RuntimeError("Expected 16-bit PCM WAV from device.")
    pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if nch == 2:
        pcm = pcm.reshape(-1, 2).mean(axis=1)
    elif nch != 1:
        raise RuntimeError("Expected mono or stereo WAV.")
    # Whisper expects 16 kHz internally; faster-whisper resamples if needed
    if sr != 16000:
        pcm = _resample_linear(pcm, sr, 16000)
    return pcm


def _resample_linear(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    if src_rate == dst_rate:
        return samples
    duration = len(samples) / float(src_rate)
    target_n = int(duration * dst_rate)
    if target_n <= 0:
        return samples
    x_old = np.linspace(0.0, 1.0, num=len(samples), endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=target_n, endpoint=False)
    return np.interp(x_new, x_old, samples).astype(np.float32)


def transcribe_wav(wav_bytes: bytes) -> str:
    model = _ensure_whisper()
    audio = _wav_bytes_to_float_mono(wav_bytes)
    segments, _ = model.transcribe(
        audio,
        language=SETTINGS.whisper_language,
        beam_size=1,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 250},
        condition_on_previous_text=False,
        log_progress=False,
    )
    text = " ".join(seg.text.strip() for seg in segments).strip()
    if not text:
        raise RuntimeError("No speech recognized from input audio.")
    return text


def process_voice_wav(wav_bytes: bytes, viewer_name: str | None = None) -> bytes:
    if not wav_bytes:
        raise RuntimeError("Empty audio.")
    if len(wav_bytes) > SETTINGS.max_request_bytes:
        raise RuntimeError("Audio exceeds size limit.")

    user_text = transcribe_wav(wav_bytes)
    if viewer_name:
        logger.info("Voice query viewer: %s | heard: %s", viewer_name, user_text[:120])
    else:
        logger.info("Voice query (no recognized viewer) | heard: %s", user_text[:120])
    reply = answer_voice_query(
        user_text,
        viewer_name=viewer_name,
        model=SETTINGS.ollama_model,
        api_url=SETTINGS.ollama_url,
        max_words=SETTINGS.max_words_reply,
    )
    wav, _voice = synthesize_sapi_wav_bytes(reply)
    return resample_wav_bytes_to_mono_16bit(wav, VOICE_ASSIST_PLAYBACK_HZ)
