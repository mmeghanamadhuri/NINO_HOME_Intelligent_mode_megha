"""STT (ElevenLabs Scribe / Whisper) + LLM (Ollama) + WAV TTS for /ws/voice and helpers."""

from __future__ import annotations

import io
import os
import random
import re
import time
import wave
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

import numpy as np
import requests

logger = logging.getLogger(__name__)

from llm_service import (
    answer_conversation_recap,
    answer_identity_question,
    answer_recap_contextual_question,
    answer_voice_query,
    DEFAULT_MODEL,
    DEFAULT_OLLAMA_URL,
    extract_recap_focus_topic,
    extract_recap_follow_up_question,
    is_assumed_prior_topic_question,
    is_conversation_recap_question,
    recap_topic_not_found_reply,
)
from memory_filters import is_likely_tts_echo, is_unintelligible_stt
from eye_expression import infer_eye_expression_for_response
from tts_service import last_tts_synthesis_info, synthesize_sapi_wav_bytes
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
        r"\bdo you know my name\b",
        r"\bdo you know who i am\b",
        r"\bwho is this\b",
        r"\bidentify me\b",
        r"\brecogni[sz]e me\b",
        r"\bwhat do you call me\b",
        r"\bwhat(?:'s| is) my identity\b",
        r"^my name[.!?…]*\s*$",
    )
)

_LOCAL_TIME_QUESTION_PATTERN = re.compile(
    r"\b(?:"
    r"what(?:'s| is)? (?:the )?time(?: now)?"
    r"|what time is it"
    r"|tell me (?:the )?time"
    r"|current time"
    r"|time now"
    r")\b",
    re.IGNORECASE,
)

_WEATHER_QUESTION_PATTERN = re.compile(
    r"\b(?:weather|forecast|temperature outside|will it rain|is it raining|"
    r"rain(?:ing)? outside|wind(?:y)? outside|outside conditions)\b",
    re.IGNORECASE,
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

_VOLUME_SET_PATTERN = re.compile(
    r"\b(?:set|change|make|keep|increase|decrease|raise|lower|turn)\b.*\bvolume\b"
    r".*?\b(?:to|at)\s+([a-z0-9\s-]+)(?:\s*percent)?\b",
    re.IGNORECASE,
)
_VOLUME_STEP_PATTERN = re.compile(
    r"\b(increase|decrease|raise|lower)\s+(?:the\s+)?(?:speaker\s+)?volume\b"
    r"(?:\s+by\s+([a-z0-9\s-]+?)(?:\s*percent)?)?\b"
    r"|\bvolume\s+(up|down)\b(?:\s+by\s+([a-z0-9\s-]+?)(?:\s*percent)?)?\b",
    re.IGNORECASE,
)
_NUMBER_WORDS: dict[str, int] = {
    "zero": 0,
    "ten": 10,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
    "hundred": 100,
}

# Seconds after TTS is sent before POST /servo/360 (lets confirmation play first).
SERVO_360_TRIGGER_DELAY_SECONDS = float(os.environ.get("SERVO_360_TRIGGER_DELAY_SECONDS", "2.0"))


@dataclass
class VoiceReplyMeta:
    trigger_servo_360: bool = False
    prompt_medical_ack: bool = False
    eye_expression: str | None = None
    registered_face_name: str | None = None
    face_reg_relisten: bool = False
    device_id: str = ""
    # Per-stage latency info for this query (stt/reply/tts seconds, heard text,
    # reply path, audio sizes). Filled by process_voice_wav; logged by app.py.
    timings: dict[str, Any] = field(default_factory=dict)


# Roughly 2–3 personalized voice replies per 10–20 (override with VOICE_PERSONALIZE_PROB).
DEFAULT_VOICE_PERSONALIZE_PROB = 0.18


@dataclass
class VoiceSettings:
    ollama_url: str = DEFAULT_OLLAMA_URL
    ollama_model: str = DEFAULT_MODEL
    whisper_model: str = "tiny"
    whisper_language: str | None = "en"
    # "elevenlabs" (cloud Scribe API; needs ELEVENLABS_API_KEY) or "whisper" (local).
    stt_provider: str = "whisper"
    elevenlabs_api_key: str = ""
    elevenlabs_stt_model: str = "scribe_v1"
    max_request_bytes: int = 512_000
    max_words_reply: int = 45
    recap_max_words: int = 55
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
    SETTINGS.elevenlabs_api_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    SETTINGS.elevenlabs_stt_model = os.environ.get(
        "ELEVENLABS_STT_MODEL", "scribe_v1"
    ).strip()
    provider = os.environ.get("STT_PROVIDER", "").strip().lower()
    if not provider:
        # Default to ElevenLabs whenever a key is available, else local Whisper.
        provider = "elevenlabs" if SETTINGS.elevenlabs_api_key else "whisper"
    SETTINGS.stt_provider = provider
    if provider == "elevenlabs" and not SETTINGS.elevenlabs_api_key:
        logger.warning(
            "STT provider is elevenlabs but ELEVENLABS_API_KEY is not set; "
            "falling back to local Whisper."
        )
        SETTINGS.stt_provider = "whisper"
    SETTINGS.personalize_prob = float(
        os.environ.get("VOICE_PERSONALIZE_PROB", str(DEFAULT_VOICE_PERSONALIZE_PROB))
    )
    SETTINGS.personalize_prob = min(1.0, max(0.0, SETTINGS.personalize_prob))
    SETTINGS.recap_max_words = max(
        30,
        min(80, int(os.environ.get("VOICE_RECAP_MAX_WORDS", "55"))),
    )


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


def _recent_assistant_replies(
    recent_history: list[tuple[str, str]] | None,
) -> list[str]:
    if not recent_history:
        return []
    return [str(assistant_text or "").strip() for _user, assistant_text in recent_history]


def _live_memory_viewer_name(
    camera_identity_name: str | None,
    camera_identity_state: CameraIdentityState,
) -> str | None:
    """Require a live recognized face before loading per-user memory context."""
    if camera_identity_state != "recognized":
        return None
    if not camera_identity_name:
        return None
    cleaned = str(camera_identity_name).strip()
    if not cleaned or cleaned.lower() in {"unknown", "face"}:
        return None
    return cleaned


def _recap_context_from_recent_turns(
    recent_history: list[tuple[str, str]],
    *,
    focus_topic: str | None = None,
) -> str | None:
    """Build recap context from latest non-recap turns (up to 5)."""
    from llm_service import recap_turn_matches_topic

    eligible: list[tuple[str, str]] = []
    for user_text, assistant_text in recent_history:
        heard = str(user_text or "").strip()
        replied = str(assistant_text or "").strip()
        if not heard:
            continue
        # For topic-focused recap, keep prior recap turns if they still contain
        # substantive assistant content about that topic (e.g. earlier speaker discussion).
        if is_conversation_recap_question(heard) and not focus_topic:
            continue
        if focus_topic and not recap_turn_matches_topic(focus_topic, heard, replied):
            continue
        eligible.append((heard, replied))

    if focus_topic and not eligible:
        return None

    lines: list[str] = []
    latest_first = list(reversed(eligible[-5:]))
    for idx, (heard, replied) in enumerate(latest_first, start=1):
        lines.append(
            f"- Latest turn {idx}: User said: {heard[:180]} | Assistant replied: {replied[:180]}"
        )
    if not lines:
        return None
    header = (
        f"Recent turns about '{focus_topic}' (newest to oldest):\n"
        if focus_topic
        else "Recent turns (ordered newest to oldest, use these for recap):\n"
    )
    return header + "\n".join(lines)


def is_identity_question(user_text: str) -> bool:
    text = user_text.strip()
    if not text:
        return False
    return any(p.search(text) for p in _IDENTITY_QUESTION_PATTERNS)


def is_local_time_question(user_text: str) -> bool:
    return bool(_LOCAL_TIME_QUESTION_PATTERN.search(user_text.strip()))


def is_weather_question(user_text: str) -> bool:
    return bool(_WEATHER_QUESTION_PATTERN.search(user_text.strip()))


def local_server_time_reply() -> str:
    """Speak the server's configured local wall-clock time, not an LLM guess."""
    now = datetime.now().astimezone()
    hour = now.strftime("%I").lstrip("0") or "0"
    return f"It is {hour}:{now.strftime('%M %p')}, {now.strftime('%A, %B')} {now.day}."


def is_servo_360_command(user_text: str) -> bool:
    text = user_text.strip()
    if not text:
        return False
    return any(p.search(text) for p in _SERVO_360_PATTERNS)


def esp_servo_360_url(device_id: str | None = None) -> str | None:
    from esp_playback import device_base_url

    base = device_base_url(device_id)
    if not base:
        return None
    return f"{base}/servo/360"


def esp_speaker_volume_url(device_id: str | None = None) -> str | None:
    from esp_playback import device_base_url

    base = device_base_url(device_id)
    if not base:
        return None
    return f"{base}/speaker/volume"


def trigger_esp_servo_360(device_id: str | None = None) -> tuple[bool, str | None]:
    """POST /servo/360 on the ESP. Returns (ok, error_code)."""
    url = esp_servo_360_url(device_id)
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


def get_esp_speaker_volume(device_id: str | None = None) -> tuple[int | None, str | None]:
    url = esp_speaker_volume_url(device_id)
    if not url:
        return None, "no_esp_url"
    try:
        resp = requests.get(url, timeout=8)
        if resp.status_code != 200:
            return None, f"http_{resp.status_code}"
        payload = resp.json()
        vol = int(payload.get("volume_percent", -1))
        if 0 <= vol <= 100:
            return vol, None
        return None, "bad_response"
    except Exception as exc:
        logger.warning("ESP speaker volume read failed: %s", exc)
        return None, "request_failed"


def set_esp_speaker_volume(
    percent: int, device_id: str | None = None
) -> tuple[int | None, str | None]:
    url = esp_speaker_volume_url(device_id)
    if not url:
        return None, "no_esp_url"
    pct = max(0, min(100, int(percent)))
    try:
        resp = requests.post(url, params={"value": str(pct)}, timeout=8)
        if resp.status_code != 200:
            return None, f"http_{resp.status_code}"
        payload = resp.json()
        applied = int(payload.get("volume_percent", pct))
        if 0 <= applied <= 100:
            return applied, None
        return None, "bad_response"
    except Exception as exc:
        logger.warning("ESP speaker volume set failed: %s", exc)
        return None, "request_failed"


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


def _parse_volume_value_phrase(raw: str) -> int | None:
    text = raw.strip().lower().replace("-", " ")
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\bpercent\b", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None
    if text in {"max", "maximum", "full"}:
        return 100
    if text in {"min", "minimum", "mute"}:
        return 0
    if text.isdigit():
        value = int(text)
        return value if 0 <= value <= 100 else None
    if text in _NUMBER_WORDS:
        return _NUMBER_WORDS[text]
    if text in {"one hundred", "a hundred"}:
        return 100
    if "hundred" in text:
        return 100
    return None


def parse_volume_command(user_text: str) -> tuple[str, int | None] | None:
    """
    Returns:
      ("set", target_percent)
      ("delta", delta_percent)
    """
    text = user_text.strip().lower()
    if not text or "volume" not in text:
        return None

    set_match = _VOLUME_SET_PATTERN.search(text)
    if set_match:
        value = _parse_volume_value_phrase(set_match.group(1))
        if value is not None:
            return ("set", value)
        return None

    step_match = _VOLUME_STEP_PATTERN.search(text)
    if not step_match:
        return None

    if step_match.group(1):
        action = step_match.group(1).lower()
        amount_raw = step_match.group(2) or ""
        sign = 1 if action in {"increase", "raise"} else -1
    else:
        direction = (step_match.group(3) or "").lower()
        amount_raw = step_match.group(4) or ""
        sign = 1 if direction == "up" else -1

    if not amount_raw.strip():
        return ("delta", sign * 10)
    amount = _parse_volume_value_phrase(amount_raw)
    if amount is None:
        return ("delta", sign * 10)
    return ("delta", sign * amount)


def apply_volume_command(
    user_text: str, *, device_id: str | None = None
) -> tuple[bool, str]:
    parsed = parse_volume_command(user_text)
    if parsed is None:
        return False, ""

    mode, value = parsed
    if mode == "set":
        applied, err = set_esp_speaker_volume(value or 0, device_id=device_id)
        if err:
            if err == "no_esp_url":
                return True, "I cannot reach the robot speaker right now."
            return True, "I could not change the volume on the robot."
        return True, f"Okay, speaker volume set to {applied} percent."

    current, err = get_esp_speaker_volume(device_id=device_id)
    if err or current is None:
        if err == "no_esp_url":
            return True, "I cannot reach the robot speaker right now."
        return True, "I could not read the current speaker volume."
    target = max(0, min(100, current + (value or 0)))
    applied, err = set_esp_speaker_volume(target, device_id=device_id)
    if err:
        return True, "I could not change the volume on the robot."
    return True, f"Okay, speaker volume set to {applied} percent."


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


def _transcribe_whisper(wav_bytes: bytes) -> str:
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


def _transcribe_elevenlabs(wav_bytes: bytes) -> str:
    """ElevenLabs Scribe speech-to-text API."""
    api_key = SETTINGS.elevenlabs_api_key
    if not api_key:
        raise RuntimeError("ELEVENLABS_API_KEY is not set.")
    data: dict[str, str] = {
        "model_id": SETTINGS.elevenlabs_stt_model,
        "tag_audio_events": "false",
    }
    if SETTINGS.whisper_language:
        data["language_code"] = SETTINGS.whisper_language
    resp = requests.post(
        "https://api.elevenlabs.io/v1/speech-to-text",
        headers={"xi-api-key": api_key},
        data=data,
        files={"file": ("voice.wav", wav_bytes, "audio/wav")},
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"ElevenLabs STT HTTP {resp.status_code}: {resp.text[:200]}"
        )
    text = str(resp.json().get("text", "")).strip()
    if not text:
        raise RuntimeError("No speech recognized from input audio.")
    return text


def transcribe_wav(wav_bytes: bytes) -> tuple[str, str]:
    """Transcribe device WAV. Returns (text, engine_used)."""
    if SETTINGS.stt_provider == "elevenlabs":
        try:
            return _transcribe_elevenlabs(wav_bytes), "elevenlabs"
        except Exception as exc:
            # Network/API hiccup must not brick the robot — fall back to local Whisper.
            logger.warning("ElevenLabs STT failed (%s); falling back to Whisper.", exc)
    return _transcribe_whisper(wav_bytes), "whisper"


def process_voice_wav(
    wav_bytes: bytes,
    viewer_name: str | None = None,
    *,
    camera_identity_name: str | None = None,
    camera_identity_state: CameraIdentityState = "no_face",
    device_id: str = "",
) -> tuple[bytes, VoiceReplyMeta]:
    meta = VoiceReplyMeta(device_id=device_id or "")
    if not wav_bytes:
        raise RuntimeError("Empty audio.")
    if len(wav_bytes) > SETTINGS.max_request_bytes:
        raise RuntimeError("Audio exceeds size limit.")

    t_start = time.perf_counter()

    user_text, stt_engine = transcribe_wav(wav_bytes)
    t_stt = time.perf_counter()
    reply_path = "llm"

    handled_volume, volume_reply = apply_volume_command(
        user_text, device_id=device_id or None
    )
    if handled_volume:
        reply_path = "volume"
        logger.info("Voice volume command | heard: %s", user_text[:120])
        reply = volume_reply
        t_reply = time.perf_counter()
        wav, _voice = synthesize_sapi_wav_bytes(reply)
        tts_info = last_tts_synthesis_info()
        wav_out = resample_wav_bytes_to_mono_16bit(wav, VOICE_ASSIST_PLAYBACK_HZ)
        t_tts = time.perf_counter()

        audio_in_seconds = max(0, len(wav_bytes) - 44) / (16000 * 2)
        audio_out_seconds = max(0, len(wav_out) - 44) / (VOICE_ASSIST_PLAYBACK_HZ * 2)
        meta.timings = {
            "heard": user_text[:200],
            "reply_text": reply[:200],
            "reply_path": reply_path,
            "audio_in_seconds": round(audio_in_seconds, 2),
            "audio_in_bytes": len(wav_bytes),
            "audio_out_seconds": round(audio_out_seconds, 2),
            "audio_out_bytes": len(wav_out),
            "stt_engine": stt_engine,
            "tts_provider": tts_info["provider"],
            "tts_voice": tts_info["voice"],
            "stt_seconds": round(t_stt - t_start, 3),
            "reply_seconds": round(t_reply - t_stt, 3),
            "tts_seconds": round(t_tts - t_reply, 3),
            "process_total_seconds": round(t_tts - t_start, 3),
        }
        logger.info(
            "Latency | stt(%s)=%.2fs reply(%s)=%.2fs tts=%.2fs total=%.2fs | in=%.1fs out=%.1fs audio",
            stt_engine,
            meta.timings["stt_seconds"],
            reply_path,
            meta.timings["reply_seconds"],
            meta.timings["tts_seconds"],
            meta.timings["process_total_seconds"],
            audio_in_seconds,
            audio_out_seconds,
        )
        return wav_out, meta

    from face_registration_service import get_face_registration_service

    face_reg = get_face_registration_service()
    awaiting_face_reg = (
        face_reg is not None and face_reg.accepts_registration_voice(user_text)
    )

    if (
        not awaiting_face_reg
        and (is_likely_tts_echo(user_text) or is_unintelligible_stt(user_text))
    ):
        reply_path = "stt_rejected"
        logger.info("Voice STT rejected (echo/garbled) | heard: %s", user_text[:120])
        reply = "Sorry, I didn't catch that. Could you say that again?"
        t_reply = time.perf_counter()
        wav, _voice = synthesize_sapi_wav_bytes(reply)
        tts_info = last_tts_synthesis_info()
        wav_out = resample_wav_bytes_to_mono_16bit(wav, VOICE_ASSIST_PLAYBACK_HZ)
        t_tts = time.perf_counter()
        audio_in_seconds = max(0, len(wav_bytes) - 44) / (16000 * 2)
        audio_out_seconds = max(0, len(wav_out) - 44) / (VOICE_ASSIST_PLAYBACK_HZ * 2)
        meta.timings = {
            "heard": user_text[:200],
            "reply_text": reply[:200],
            "reply_path": reply_path,
            "audio_in_seconds": round(audio_in_seconds, 2),
            "audio_in_bytes": len(wav_bytes),
            "audio_out_seconds": round(audio_out_seconds, 2),
            "audio_out_bytes": len(wav_out),
            "stt_engine": stt_engine,
            "tts_provider": tts_info["provider"],
            "tts_voice": tts_info["voice"],
            "stt_seconds": round(t_stt - t_start, 3),
            "reply_seconds": round(t_reply - t_stt, 3),
            "tts_seconds": round(t_tts - t_reply, 3),
            "process_total_seconds": round(t_tts - t_start, 3),
        }
        return wav_out, meta

    from alarm_voice import handle_alarm_voice

    from memory_service import get_memory_service, resolve_alarm_user

    memory_svc = get_memory_service()
    memory_name = _live_memory_viewer_name(
        camera_identity_name,
        camera_identity_state,
    )
    memory_ctx = None
    if memory_name:
        memory_ctx = memory_svc.load_context(memory_name, query_text=user_text)
    alarm_user_id, alarm_person_name = resolve_alarm_user(
        camera_identity_name,
        camera_identity_state,
    )
    t_memory = time.perf_counter()

    face_reg = get_face_registration_service()
    if face_reg is not None and face_reg.accepts_registration_voice(user_text):
        reg_result = face_reg.handle_voice(user_text)
        if reg_result.handled:
            reply_path = "face_registration"
            reply = reg_result.reply
            if reg_result.registered_name:
                meta.registered_face_name = reg_result.registered_name
            if reg_result.relisten_after_reply:
                meta.face_reg_relisten = True
            logger.info(
                "Voice face registration | registered=%s heard: %s",
                reg_result.registered_name or "(none)",
                user_text[:120],
            )
            if reg_result.relisten_after_reply:
                audio_in_seconds = max(0, len(wav_bytes) - 44) / (16000 * 2)
                meta.timings = {
                    "heard": user_text[:200],
                    "reply_text": "(face registration relisten scheduled)",
                    "reply_path": reply_path,
                    "audio_in_seconds": round(audio_in_seconds, 2),
                    "audio_in_bytes": len(wav_bytes),
                    "stt_engine": stt_engine,
                    "stt_seconds": round(t_stt - t_start, 3),
                }
                return b"", meta

    if reply_path == "llm" and is_local_time_question(user_text):
        reply_path = "local_time"
        reply = local_server_time_reply()
        logger.info("Voice local-time query | heard: %s", user_text[:120])

    if reply_path == "llm" and is_weather_question(user_text):
        from device_registry import get_device_registry
        from weather_service import (
            DeviceLocationUnavailableError,
            WeatherUnavailableError,
            get_weather_service,
            weather_voice_reply,
        )

        reply_path = "weather"
        device = get_device_registry().resolve_or_default(device_id or None)
        try:
            reply = weather_voice_reply(
                device, get_weather_service().current_for_device(device)
            )
            logger.info(
                "Voice weather query | device=%s heard: %s",
                device.device_id,
                user_text[:120],
            )
        except DeviceLocationUnavailableError:
            reply = (
                f"I do not have a location configured for {device.display_name or device.device_id}. "
                "Please set its location first."
            )
        except WeatherUnavailableError:
            reply = "I cannot retrieve the current weather right now. Please try again soon."

    if reply_path == "llm":
        alarm_result = handle_alarm_voice(
            user_text,
            user_id=alarm_user_id,
            person_name=alarm_person_name,
            camera_identity_name=camera_identity_name,
            camera_identity_state=camera_identity_state,
            device_id=device_id,
        )
        if alarm_result.handled:
            reply_path = "alarm"
            logger.info("Voice alarm command | heard: %s", user_text[:120])
            reply = alarm_result.reply
            from alarm_service import get_alarm_service

            if get_alarm_service().get_reschedule_prompt_alarm() is not None:
                meta.prompt_medical_ack = True
    if reply_path == "llm" and is_servo_360_command(user_text):
        reply_path = "servo_360"
        logger.info("Voice servo 360 command | heard: %s", user_text[:120])
        if esp_servo_360_url(device_id or None) is None:
            reply = reply_for_servo_360_command(error="no_esp_url")
        else:
            meta.trigger_servo_360 = True
            reply = reply_for_servo_360_command()
    if reply_path == "llm" and is_conversation_recap_question(user_text):
        if not memory_name:
            reply_path = "recap_blocked_no_face"
            logger.info(
                "Voice recap blocked (no live recognized face) | state=%s heard: %s",
                camera_identity_state,
                user_text[:120],
            )
            reply = answer_conversation_recap(
                user_text,
                viewer_name=None,
                recognition_state=camera_identity_state,
                model=SETTINGS.ollama_model,
                api_url=SETTINGS.ollama_url,
                max_words=SETTINGS.recap_max_words,
                memory_context=None,
            )
        else:
            focus_topic = extract_recap_focus_topic(user_text)
            assumed_prior_topic = is_assumed_prior_topic_question(user_text)
            recap_context = _recap_context_from_recent_turns(
                memory_ctx.recent_history if memory_ctx else [],
                focus_topic=focus_topic if assumed_prior_topic else None,
            )
            if assumed_prior_topic:
                if not focus_topic or recap_context is None:
                    reply_path = "recap_not_found"
                    logger.info(
                        "Voice recap topic not in history | viewer=%s topic=%s heard: %s",
                        memory_name,
                        focus_topic or "(unknown)",
                        user_text[:120],
                    )
                    reply = recap_topic_not_found_reply(
                        focus_topic or "that topic",
                        person_name=memory_name,
                    )
                else:
                    follow_up = extract_recap_follow_up_question(user_text)
                    if follow_up:
                        reply_path = "recap_answer"
                        logger.info(
                            "Voice recap + question | viewer=%s topic=%s follow_up=%s heard: %s",
                            memory_name,
                            focus_topic,
                            follow_up[:80],
                            user_text[:120],
                        )
                        reply = answer_recap_contextual_question(
                            user_text,
                            follow_up,
                            viewer_name=memory_name,
                            memory_context=recap_context,
                            focus_topic=focus_topic,
                            model=SETTINGS.ollama_model,
                            api_url=SETTINGS.ollama_url,
                            max_words=max(SETTINGS.recap_max_words, SETTINGS.max_words_reply),
                        )
                    else:
                        reply_path = "recap"
                        logger.info(
                            "Voice recap topic confirmed | viewer=%s topic=%s heard: %s",
                            memory_name,
                            focus_topic,
                            user_text[:120],
                        )
                        reply = answer_conversation_recap(
                            user_text,
                            viewer_name=memory_name,
                            recognition_state=camera_identity_state,
                            model=SETTINGS.ollama_model,
                            api_url=SETTINGS.ollama_url,
                            max_words=SETTINGS.recap_max_words,
                            memory_context=recap_context,
                            focus_topic=focus_topic,
                        )
            else:
                follow_up = extract_recap_follow_up_question(user_text)
                if follow_up and recap_context:
                    reply_path = "recap_answer"
                    logger.info(
                        "Voice recap + question (general) | viewer=%s follow_up=%s heard: %s",
                        memory_name,
                        follow_up[:80],
                        user_text[:120],
                    )
                    reply = answer_recap_contextual_question(
                        user_text,
                        follow_up,
                        viewer_name=memory_name,
                        memory_context=recap_context,
                        focus_topic=focus_topic,
                        model=SETTINGS.ollama_model,
                        api_url=SETTINGS.ollama_url,
                        max_words=max(SETTINGS.recap_max_words, SETTINGS.max_words_reply),
                    )
                else:
                    reply_path = "recap"
                    logger.info(
                        "Voice recap query | viewer=%s turns=%s topic=%s | heard: %s",
                        memory_name,
                        memory_ctx.recent_turns if memory_ctx else 0,
                        focus_topic or "(general)",
                        user_text[:120],
                    )
                    reply = answer_conversation_recap(
                        user_text,
                        viewer_name=memory_name,
                        recognition_state=camera_identity_state,
                        model=SETTINGS.ollama_model,
                        api_url=SETTINGS.ollama_url,
                        max_words=SETTINGS.recap_max_words,
                        memory_context=recap_context,
                        focus_topic=focus_topic,
                    )
    elif reply_path == "llm" and memory_name and memory_ctx and memory_svc.ready:
        llm_memory = memory_svc.handle_llm_memory_turn(
            memory_ctx.user_id,
            user_text,
            person_name=memory_name,
            model=SETTINGS.ollama_model,
            api_url=SETTINGS.ollama_url,
        )
        if llm_memory:
            reply_path, reply = llm_memory
            logger.info(
                "Voice LLM memory | path=%s viewer=%s heard: %s",
                reply_path,
                memory_name,
                user_text[:120],
            )
    if reply_path == "llm" and is_identity_question(user_text):
        reply_path = "identity_llm"
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
            memory_context=memory_ctx.prompt_block if memory_ctx else None,
        )
    elif reply_path == "llm":
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
            memory_context=memory_ctx.prompt_block if memory_ctx else None,
            recent_assistant_replies=_recent_assistant_replies(
                memory_ctx.recent_history if memory_ctx else None
            ),
        )
    t_reply = time.perf_counter()

    memory_store = "skipped"
    if reply_path in {"llm", "identity_llm", "memory_llm_store", "recap_answer"}:
        memory_store = memory_svc.log_conversation_for_viewer(
            memory_name,
            user_text,
            reply,
            existing=memory_ctx,
            reply_path=reply_path,
        )

    meta.eye_expression = infer_eye_expression_for_response(
        reply,
        user_text=user_text,
        reply_path=reply_path,
    )

    wav, _voice = synthesize_sapi_wav_bytes(reply)
    tts_info = last_tts_synthesis_info()
    wav_out = resample_wav_bytes_to_mono_16bit(wav, VOICE_ASSIST_PLAYBACK_HZ)
    t_tts = time.perf_counter()

    # Input is 16 kHz 16-bit mono WAV from the device (44-byte header).
    audio_in_seconds = max(0, len(wav_bytes) - 44) / (16000 * 2)
    audio_out_seconds = max(0, len(wav_out) - 44) / (VOICE_ASSIST_PLAYBACK_HZ * 2)
    meta.timings = {
        "heard": user_text[:200],
        "reply_text": reply[:200],
        "reply_path": reply_path,
        "audio_in_seconds": round(audio_in_seconds, 2),
        "audio_in_bytes": len(wav_bytes),
        "audio_out_seconds": round(audio_out_seconds, 2),
        "audio_out_bytes": len(wav_out),
        "stt_engine": stt_engine,
        "tts_provider": tts_info["provider"],
        "tts_voice": tts_info["voice"],
        "stt_seconds": round(t_stt - t_start, 3),
        "memory_seconds": round(t_memory - t_stt, 3),
        "reply_seconds": round(t_reply - t_memory, 3),
        "tts_seconds": round(t_tts - t_reply, 3),
        "process_total_seconds": round(t_tts - t_start, 3),
        "voice_viewer": viewer_name or "",
        "memory_viewer": memory_name or "",
        "memory_ready": memory_svc.ready,
        "memory_store": memory_store,
    }
    if memory_ctx:
        meta.timings["memory_turns"] = memory_ctx.recent_turns
        meta.timings["memory_facts"] = memory_ctx.memory_count
        meta.timings["memory_user"] = memory_ctx.name
    if meta.eye_expression:
        meta.timings["eye_expression"] = meta.eye_expression
    logger.info(
        "Latency | stt(%s)=%.2fs reply(%s)=%.2fs tts=%.2fs total=%.2fs | in=%.1fs out=%.1fs%s",
        stt_engine,
        meta.timings["stt_seconds"],
        reply_path,
        meta.timings["reply_seconds"],
        meta.timings["tts_seconds"],
        meta.timings["process_total_seconds"],
        audio_in_seconds,
        audio_out_seconds,
        f" | eye={meta.eye_expression}" if meta.eye_expression else "",
    )
    return wav_out, meta
