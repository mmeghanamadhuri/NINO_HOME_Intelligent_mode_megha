"""STT (Whisper) + LLM (Ollama) + WAV TTS for /ws/voice and helpers."""

from __future__ import annotations

import io
import os
import random
import re
import wave
import logging
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlparse

import numpy as np
import requests

logger = logging.getLogger(__name__)

from llm_service import (
    answer_identity_question,
    answer_voice_query,
    DEFAULT_MODEL,
    DEFAULT_OLLAMA_URL,
)
from tts_service import synthesize_sapi_wav_bytes
from wav_resample import resample_wav_bytes_to_mono_16bit

# Voice assistant path uses 16 kHz on device (ESP-SR WakeNet + VAD); face TTS stays 22050 in tts_service.
VOICE_ASSIST_PLAYBACK_HZ = 16000

CameraIdentityState = Literal["recognized", "unknown", "no_face"]

_IDENTITY_QUESTION_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bwho am i\b",
        r"\bwhat(?:'s| is) my name\b",
        r"\bdo you know me\b",
        r"\bdo you know who i am\b",
        r"\bwho is this\b",
        r"\bidentify me\b",
        r"\brecogni[sz]e me\b",
        r"\bwhat do you call me\b",
        r"\bwhat(?:'s| is) my identity\b",
    )
)

_SERVO_360_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bmake a 360\b",
        r"\bdo a 360\b",
        r"\bmake (?:a )?360\b",
        r"\bdo (?:a )?360\b",
        r"\bspin 360\b",
        r"\bspin a 360\b",
        r"\b(?:make|do) (?:a )?(?:three[\s-]?sixty|360)\b",
        r"\b(?:spin|rotate|turn)(?: around)? (?:a )?360\b",
        r"\b360 (?:degree|degrees|spin|rotation)\b",
        r"\bfull 360\b",
        r"\b(?:servo|motor|head).{0,20}(?:spin|rotate|360)\b",
        r"\b(?:spin|rotate|360).{0,20}(?:servo|motor|head)\b",
    )
)

# Seconds after TTS is sent before POST /servo/360 (lets confirmation play first).
SERVO_360_TRIGGER_DELAY_SECONDS = float(os.environ.get("SERVO_360_TRIGGER_DELAY_SECONDS", "2.0"))


@dataclass
class VoiceReplyMeta:
    trigger_servo_360: bool = False
    prompt_medical_ack: bool = False


# Roughly 2–3 personalized voice replies per 10–20 (override with VOICE_PERSONALIZE_PROB).
DEFAULT_VOICE_PERSONALIZE_PROB = 0.18


@dataclass
class VoiceSettings:
    ollama_url: str = DEFAULT_OLLAMA_URL
    ollama_model: str = DEFAULT_MODEL
    whisper_model: str = "tiny"
    whisper_language: str | None = "en"
    max_request_bytes: int = 512_000
    max_words_reply: int = 55
    personalize_prob: float = DEFAULT_VOICE_PERSONALIZE_PROB


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
    SETTINGS.personalize_prob = float(
        os.environ.get("VOICE_PERSONALIZE_PROB", str(DEFAULT_VOICE_PERSONALIZE_PROB))
    )
    SETTINGS.personalize_prob = min(1.0, max(0.0, SETTINGS.personalize_prob))


def _viewer_for_this_reply(viewer_name: str | None) -> str | None:
    """Randomly include the camera viewer name (~2–3 of every 10–20 voice replies)."""
    if not viewer_name:
        return None
    cleaned = viewer_name.strip()
    if not cleaned or cleaned.lower() in {"unknown", "face"}:
        return None
    if random.random() < SETTINGS.personalize_prob:
        return cleaned
    return None


def is_identity_question(user_text: str) -> bool:
    text = user_text.strip()
    if not text:
        return False
    return any(p.search(text) for p in _IDENTITY_QUESTION_PATTERNS)


def is_servo_360_command(user_text: str) -> bool:
    text = user_text.strip()
    if not text:
        return False
    return any(p.search(text) for p in _SERVO_360_PATTERNS)


def esp_servo_360_url() -> str | None:
    play_url = os.environ.get("ESP_PLAY_WAV_URL", "").strip()
    if not play_url:
        return None
    parsed = urlparse(play_url)
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}/servo/360"


def trigger_esp_servo_360() -> tuple[bool, str | None]:
    """POST /servo/360 on the ESP. Returns (ok, error_code)."""
    url = esp_servo_360_url()
    if not url:
        return False, "no_esp_url"
    try:
        resp = requests.post(url, timeout=8)
        if resp.status_code == 200:
            return True, None
        try:
            payload = resp.json()
            err = str(payload.get("error", "request_failed"))
        except Exception:
            err = f"http_{resp.status_code}"
        logger.warning("ESP servo 360 failed: %s %s", resp.status_code, err)
        return False, err
    except requests.RequestException as exc:
        logger.warning("ESP servo 360 request failed: %s", exc)
        return False, "request_failed"


def reply_for_servo_360_command(*, error: str | None = None) -> str:
    """Fixed spoken reply for servo 360 voice commands — no LLM."""
    if error == "no_esp_url":
        return (
            "I cannot reach the robot. "
            "Set ESP play WAV URL on the server to the board IP."
        )
    if error == "servos_not_ready":
        return "The servos are not ready. Connect the U2D2 on the USB hub and power the motors."
    if error == "already_running":
        return "A spin is already running."
    if error == "request_failed":
        return "I tried to start the spin but the robot did not respond."
    return "OK, doing the spin now."


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


def process_voice_wav(
    wav_bytes: bytes,
    viewer_name: str | None = None,
    *,
    camera_identity_name: str | None = None,
    camera_identity_state: CameraIdentityState = "no_face",
) -> tuple[bytes, VoiceReplyMeta]:
    meta = VoiceReplyMeta()
    if not wav_bytes:
        raise RuntimeError("Empty audio.")
    if len(wav_bytes) > SETTINGS.max_request_bytes:
        raise RuntimeError("Audio exceeds size limit.")

    user_text = transcribe_wav(wav_bytes)

    from alarm_voice import handle_alarm_voice

    alarm_result = handle_alarm_voice(
        user_text,
        viewer_name=viewer_name,
        camera_identity_name=camera_identity_name,
        camera_identity_state=camera_identity_state,
    )
    if alarm_result.handled:
        logger.info("Voice alarm command | heard: %s", user_text[:120])
        reply = alarm_result.reply
        from alarm_service import get_alarm_service

        if get_alarm_service().get_reschedule_prompt_alarm() is not None:
            meta.prompt_medical_ack = True
    elif is_servo_360_command(user_text):
        logger.info("Voice servo 360 command | heard: %s", user_text[:120])
        if esp_servo_360_url() is None:
            reply = reply_for_servo_360_command(error="no_esp_url")
        else:
            meta.trigger_servo_360 = True
            reply = reply_for_servo_360_command()
    elif is_identity_question(user_text):
        logger.info(
            "Voice identity query | state=%s name=%s | heard: %s",
            camera_identity_state,
            camera_identity_name or "(none)",
            user_text[:120],
        )
        reply = answer_identity_question(
            user_text,
            registered_name=camera_identity_name,
            recognition_state=camera_identity_state,
            model=SETTINGS.ollama_model,
            api_url=SETTINGS.ollama_url,
            max_words=SETTINGS.max_words_reply,
        )
    else:
        effective_viewer = _viewer_for_this_reply(viewer_name)
        if effective_viewer:
            logger.info(
                "Voice query (personalized) viewer: %s | heard: %s",
                effective_viewer,
                user_text[:120],
            )
        elif viewer_name:
            logger.info(
                "Voice query (generic; %s in frame) | heard: %s",
                viewer_name.strip(),
                user_text[:120],
            )
        else:
            logger.info("Voice query (no recognized viewer) | heard: %s", user_text[:120])
        reply = answer_voice_query(
            user_text,
            viewer_name=effective_viewer,
            model=SETTINGS.ollama_model,
            api_url=SETTINGS.ollama_url,
            max_words=SETTINGS.max_words_reply,
        )
    wav, _voice = synthesize_sapi_wav_bytes(reply)
    return resample_wav_bytes_to_mono_16bit(wav, VOICE_ASSIST_PLAYBACK_HZ), meta
