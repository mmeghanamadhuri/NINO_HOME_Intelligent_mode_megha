from __future__ import annotations

import io
import logging
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import requests

from esp_wav_chunking import chunk_text_for_esp_limit
from wav_resample import ESP_PCM_SAMPLE_RATE_HZ, resample_wav_bytes_to_mono_16bit

logger = logging.getLogger(__name__)

_GREETING_MAX_WORDS = 40

# Emoji / pictograph ranges + ZWJ / variation selectors used in emoji sequences.
_TTS_EMOJI_RE = re.compile(
    "["
    "\U0001F1E0-\U0001F1FF"  # flags
    "\U0001F300-\U0001F5FF"  # symbols & pictographs
    "\U0001F600-\U0001F64F"  # emoticons
    "\U0001F680-\U0001F6FF"  # transport & map
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"  # supplemental symbols
    "\U0001FA00-\U0001FAFF"  # symbols extended-A
    "\U00002600-\U000026FF"  # misc symbols
    "\U00002700-\U000027BF"  # dingbats
    "\U0000FE0E\U0000FE0F"  # variation selectors
    "\U0000200D"  # zero-width joiner
    "\U000020E3"  # combining enclosing keycap
    "]+"
)
# LLM shortcodes like :smile: / :thumbs_up:
_TTS_EMOJI_SHORTCODE_RE = re.compile(r":[a-z0-9_+-]+:", re.I)
# Decorative bullets / stars / hearts that TTS would misread.
_TTS_DECORATIVE_RE = re.compile(r"[•·▪▸►★☆✦✧♥♡❤✨]+")
_TTS_MARKDOWN_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_TTS_MARKDOWN_ITALIC_RE = re.compile(r"(?<!\w)\*([^*]+)\*(?!\w)")
_TTS_MARKDOWN_UNDER_BOLD_RE = re.compile(r"__(.+?)__")
_TTS_MARKDOWN_CODE_RE = re.compile(r"`([^`]+)`")


def _split_invite_question(text: str) -> tuple[str, str | None]:
    """Return (body, invite) when text ends with a spoken invitation question."""
    text = text.strip()
    q_pos = text.rfind("?")
    if q_pos < 0:
        return text, None
    start = max(text.rfind(".", 0, q_pos), text.rfind("!", 0, q_pos))
    invite = (
        text[start + 1 : q_pos + 1].strip()
        if start >= 0
        else text[: q_pos + 1].strip()
    )
    body = text[: start + 1].strip() if start >= 0 else ""
    return body, invite or None


def _clamp_spoken_words(
    text: str, max_words: int = _GREETING_MAX_WORDS, *, preserve_invite: bool = False
) -> str:
    text = text.strip()
    words = text.split()
    if len(words) <= max_words:
        return text
    if preserve_invite and "?" in text:
        body, invite = _split_invite_question(text)
        if invite:
            invite_words = invite.split()
            body_words = body.split() if body else []
            body_budget = max(8, max_words - len(invite_words))
            if len(body_words) > body_budget:
                body_words = body_words[:body_budget]
            body = " ".join(body_words).rstrip(".,;:!?")
            if body:
                return f"{body}. {invite}"
            return invite
    return " ".join(words[:max_words]).rstrip(".,;:") + "?"


DEFAULT_ELEVENLABS_TTS_VOICE_ID = "f1K8uOKtx0TAmtXBiLqx"
DEFAULT_ELEVENLABS_TTS_MODEL = "eleven_flash_v2_5"
DEFAULT_ELEVENLABS_TTS_OUTPUT_FORMAT = "pcm_16000"
DEFAULT_ELEVENLABS_TTS_SAMPLE_RATE_HZ = 16000
DEFAULT_PIPER_MODEL_PATH = (
    Path(__file__).resolve().parent
    / "models"
    / "en_GB-southern_english_female-low.onnx"
)

_ESPEAK_LOCK = threading.Lock()
_ESPEAK_READY = False
_ESPEAK_SAMPLE_RATE_HZ = 22050
_PIPER_LOCK = threading.RLock()
_PIPER_VOICE: Any | None = None
_PIPER_VOICE_MODEL_PATH: Path | None = None
_SYNTHESIS_INFO = threading.local()
_ELEVENLABS_FALLBACK_LOCK = threading.Lock()
_ELEVENLABS_FALLBACK_UNTIL = 0.0
# espeak-ng female variants (+f3 = soft British female). Use short names; gmw/en-gb+f3 fails.
_PREFERRED_ESPEAK_VOICES = (
    "en+f3",
    "en+f4",
    "en+f2",
    "en+f5",
    "en-gb+f4",
)


def _normalize_tts_text(text: str) -> str:
    """Normalize LLM reply text for speech: quotes, strip emojis/markdown junk."""
    if not text:
        return ""
    clean = (
        text.replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201B", "'")
        .replace("\u201C", '"')
        .replace("\u201D", '"')
    )
    clean = _TTS_MARKDOWN_BOLD_RE.sub(r"\1", clean)
    clean = _TTS_MARKDOWN_UNDER_BOLD_RE.sub(r"\1", clean)
    clean = _TTS_MARKDOWN_CODE_RE.sub(r"\1", clean)
    clean = _TTS_MARKDOWN_ITALIC_RE.sub(r"\1", clean)
    clean = _TTS_EMOJI_RE.sub("", clean)
    clean = _TTS_EMOJI_SHORTCODE_RE.sub("", clean)
    clean = _TTS_DECORATIVE_RE.sub("", clean)
    clean = re.sub(r"[ \t]+", " ", clean)
    clean = re.sub(r" ?\n ?", " ", clean)
    return clean.strip()


def _use_windows_sapi() -> bool:
    return sys.platform == "win32" and shutil.which("powershell") is not None


def _elevenlabs_api_key() -> str:
    return os.environ.get("ELEVENLABS_API_KEY", "").strip()


def _elevenlabs_fallback_cooldown_seconds() -> float:
    raw = os.environ.get("ELEVENLABS_TTS_FALLBACK_COOLDOWN_SECONDS", "60").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        logger.warning(
            "Invalid ELEVENLABS_TTS_FALLBACK_COOLDOWN_SECONDS %r; using 60 seconds.",
            raw,
        )
        return 60.0


def _elevenlabs_fallback_active() -> bool:
    with _ELEVENLABS_FALLBACK_LOCK:
        return time.monotonic() < _ELEVENLABS_FALLBACK_UNTIL


def _mark_elevenlabs_unavailable() -> None:
    global _ELEVENLABS_FALLBACK_UNTIL

    cooldown = _elevenlabs_fallback_cooldown_seconds()
    with _ELEVENLABS_FALLBACK_LOCK:
        _ELEVENLABS_FALLBACK_UNTIL = time.monotonic() + cooldown


def _tts_provider() -> str:
    provider = os.environ.get("TTS_PROVIDER", "").strip().lower()
    if provider in {"elevenlabs", "piper", "sapi", "local"}:
        return provider
    if _elevenlabs_api_key():
        return "elevenlabs"
    if _piper_available():
        return "piper"
    if _use_windows_sapi():
        return "sapi"
    return "local"


def _piper_model_path() -> Path:
    configured = os.environ.get("PIPER_MODEL_PATH", "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_PIPER_MODEL_PATH


def _piper_length_scale() -> float:
    raw = os.environ.get("PIPER_LENGTH_SCALE", "1.0").strip()
    try:
        return max(0.5, min(2.0, float(raw)))
    except ValueError:
        logger.warning("Invalid PIPER_LENGTH_SCALE %r; using 1.0.", raw)
        return 1.0


def _piper_available() -> bool:
    model_path = _piper_model_path()
    if not model_path.is_file() or not Path(f"{model_path}.json").is_file():
        return False
    try:
        from piper import PiperVoice  # noqa: F401
    except ImportError:
        return False
    return True


def _load_piper_voice() -> Any:
    """Return the cached Piper model, loading it once per configured voice."""
    global _PIPER_VOICE, _PIPER_VOICE_MODEL_PATH

    model_path = _piper_model_path().resolve()
    config_path = Path(f"{model_path}.json")
    if not model_path.is_file() or not config_path.is_file():
        raise RuntimeError(
            f"Piper voice model or config is missing: {model_path} (.onnx and .onnx.json required)."
        )

    with _PIPER_LOCK:
        if _PIPER_VOICE is not None and _PIPER_VOICE_MODEL_PATH == model_path:
            return _PIPER_VOICE
        try:
            from piper import PiperVoice
        except ImportError as exc:
            raise RuntimeError("Piper Python package is not installed.") from exc
        logger.info("Loading Piper voice model: %s", model_path)
        _PIPER_VOICE = PiperVoice.load(model_path, config_path)
        _PIPER_VOICE_MODEL_PATH = model_path
        return _PIPER_VOICE


def preload_piper_voice() -> bool:
    """Warm the local Piper fallback without changing the active TTS provider."""
    enabled = os.environ.get("PIPER_PRELOAD", "1").strip().lower()
    if enabled in {"0", "false", "no", "off"}:
        logger.info("Piper preload disabled by PIPER_PRELOAD.")
        return False
    if not _piper_available():
        logger.warning("Piper fallback is unavailable; skipping preload.")
        return False
    try:
        _load_piper_voice()
        logger.info("Piper fallback preloaded: %s", _piper_model_path().stem)
        return True
    except Exception as exc:
        logger.warning("Piper fallback preload failed: %s", exc)
        return False


def _set_tts_synthesis_info(provider: str, voice: str) -> None:
    _SYNTHESIS_INFO.value = {"provider": provider, "voice": voice}


def last_tts_synthesis_info() -> dict[str, str]:
    """Provider and voice used by the current thread's latest synthesis."""
    info = getattr(_SYNTHESIS_INFO, "value", {})
    return {
        "provider": str(info.get("provider", "")),
        "voice": str(info.get("voice", "")),
    }


def _synthesize_piper_wav_bytes(
    text: str, rate: int = 135, volume: float = 0.75
) -> tuple[bytes, str]:
    """Piper local neural TTS → WAV bytes using an in-memory model."""
    del rate
    model_path = _piper_model_path()
    clean = _normalize_tts_text(text).strip()
    if not clean:
        raise RuntimeError("TTS text is empty.")

    try:
        from piper import SynthesisConfig
    except ImportError as exc:
        raise RuntimeError("Piper Python package is not installed.") from exc

    voice = _load_piper_voice()
    output = io.BytesIO()
    with _PIPER_LOCK:
        with wave.open(output, "wb") as wav_file:
            voice.synthesize_wav(
                clean,
                wav_file,
                SynthesisConfig(
                    length_scale=_piper_length_scale(),
                    volume=max(0.0, min(1.0, volume)),
                ),
            )
    wav = output.getvalue()
    if not wav:
        raise RuntimeError("Piper produced no audio.")
    return wav, model_path.stem


def _synthesize_local_fallback_wav_bytes(
    text: str, rate: int, volume: float
) -> tuple[bytes, str, str]:
    """Use the warm Piper fallback, then the platform's system TTS."""
    try:
        wav, voice = _synthesize_piper_wav_bytes(text, rate=rate, volume=volume)
        return wav, voice, "piper"
    except Exception as piper_exc:
        logger.warning("Piper TTS fallback failed (%s); using system TTS.", piper_exc)
    if _use_windows_sapi():
        wav, voice = _synthesize_windows_sapi_wav_bytes(
            text, rate=rate, volume=volume
        )
        return wav, voice, "sapi"
    wav, voice = _synthesize_espeak_wav_bytes(text, rate=rate, volume=volume)
    return wav, voice, "local"


def _elevenlabs_voice_settings() -> dict[str, Any]:
    def _pct(name: str, default: float) -> float:
        raw = os.environ.get(name, "").strip()
        if not raw:
            return default
        if raw.endswith("%"):
            return max(0.0, min(1.0, float(raw[:-1]) / 100.0))
        value = float(raw)
        return max(0.0, min(1.0, value / 100.0 if value > 1.0 else value))

    speaker_boost = os.environ.get("ELEVENLABS_TTS_SPEAKER_BOOST", "0").strip().lower()
    return {
        "stability": _pct("ELEVENLABS_TTS_STABILITY", 0.30),
        "similarity_boost": _pct("ELEVENLABS_TTS_SIMILARITY", 0.0),
        "style": _pct("ELEVENLABS_TTS_STYLE", 0.20),
        "use_speaker_boost": speaker_boost in {"1", "true", "yes", "on"},
        "speed": float(os.environ.get("ELEVENLABS_TTS_SPEED", "0.86")),
    }


def _pcm16_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    bio = io.BytesIO()
    with wave.open(bio, "wb") as wo:
        wo.setnchannels(1)
        wo.setsampwidth(2)
        wo.setframerate(sample_rate)
        wo.writeframes(pcm)
    return bio.getvalue()


def _elevenlabs_sample_rate_hz(output_format: str) -> int:
    if output_format.startswith("pcm_") and output_format[4:].isdigit():
        return int(output_format[4:])
    return DEFAULT_ELEVENLABS_TTS_SAMPLE_RATE_HZ


def _synthesize_elevenlabs_wav_bytes(
    text: str, rate: int = 135, volume: float = 0.75
) -> tuple[bytes, str]:
    """ElevenLabs cloud TTS → 16-bit mono WAV bytes. Returns (wav, voice_name)."""
    api_key = _elevenlabs_api_key()
    if not api_key:
        raise RuntimeError("ELEVENLABS_API_KEY is not set.")

    voice_id = os.environ.get(
        "ELEVENLABS_TTS_VOICE_ID", DEFAULT_ELEVENLABS_TTS_VOICE_ID
    ).strip()
    model_id = os.environ.get(
        "ELEVENLABS_TTS_MODEL", DEFAULT_ELEVENLABS_TTS_MODEL
    ).strip()
    output_format = os.environ.get(
        "ELEVENLABS_TTS_OUTPUT_FORMAT", DEFAULT_ELEVENLABS_TTS_OUTPUT_FORMAT
    ).strip()
    clean = _normalize_tts_text(text).strip()
    if not clean:
        raise RuntimeError("TTS text is empty.")

    url = (
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
        f"?output_format={output_format}"
    )
    payload = {
        "text": clean,
        "model_id": model_id,
        "voice_settings": _elevenlabs_voice_settings(),
    }
    resp = requests.post(
        url,
        headers={
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "application/octet-stream",
        },
        json=payload,
        timeout=60,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"ElevenLabs TTS HTTP {resp.status_code}: {resp.text[:200]}"
        )

    sample_rate = _elevenlabs_sample_rate_hz(output_format)
    if output_format.startswith("pcm_"):
        wav = _pcm16_to_wav(resp.content, sample_rate)
    else:
        wav = resp.content
    return wav, voice_id


def _sapi_rate_value(rate: int) -> int:
    if rate <= 120:
        return -4
    if rate <= 140:
        return -3
    if rate <= 160:
        return -1
    if rate <= 180:
        return 1
    if rate <= 200:
        return 2
    return 3


def _ensure_espeak() -> Any:
    """Initialize espeak-ng once (via pyttsx3's ctypes bindings)."""
    global _ESPEAK_READY, _ESPEAK_SAMPLE_RATE_HZ
    from pyttsx3.drivers import _espeak as espeak

    with _ESPEAK_LOCK:
        if not _ESPEAK_READY:
            sample_rate = espeak.Initialize(espeak.AUDIO_OUTPUT_RETRIEVAL, 1000)
            if sample_rate == -1:
                raise RuntimeError("could not initialize espeak-ng")
            _ESPEAK_SAMPLE_RATE_HZ = sample_rate
            _ESPEAK_READY = True
    return espeak


def _espeak_rate_value(rate: int) -> int:
    soft_rate = int(os.environ.get("LOCAL_TTS_RATE", str(max(80, rate - 12))))
    return max(80, min(450, soft_rate))


def _set_espeak_voice(espeak: Any) -> str:
    """Select a soft female British voice. Returns the active voice identifier."""
    custom = os.environ.get("LOCAL_TTS_VOICE", "").strip()
    candidates: list[str] = []
    if custom:
        candidates.append(custom)
    candidates.extend(_PREFERRED_ESPEAK_VOICES)

    for voice_id in candidates:
        if not voice_id:
            continue
        if espeak.SetVoiceByName(voice_id.encode("utf-8")) != 0:
            continue
        current = espeak.GetCurrentVoice()
        if not current:
            continue
        ident = (current.contents.identifier or b"").decode("utf-8")
        if custom and voice_id == custom:
            return ident or voice_id
        if "+f" in ident:
            return ident

    if custom:
        logger.warning(
            "LOCAL_TTS_VOICE %r unavailable; no female espeak voice found.", custom
        )
    raise RuntimeError("No female espeak voice available")


def _synthesize_espeak_wav_bytes(
    text: str, rate: int = 135, volume: float = 0.75
) -> tuple[bytes, str]:
    """Linux/macOS TTS via espeak-ng (direct) → WAV bytes. Returns (wav, voice_name)."""
    import ctypes

    espeak = _ensure_espeak()
    clean = _normalize_tts_text(text).strip()
    if not clean:
        raise RuntimeError("TTS text is empty.")

    with _ESPEAK_LOCK:
        voice_name = _set_espeak_voice(espeak)
        espeak.SetParameter(espeak.RATE, _espeak_rate_value(rate), 0)
        espeak.SetParameter(
            espeak.VOLUME, int(max(0.0, min(1.0, volume)) * 100), 0
        )

        chunks: list[bytes] = []

        def _on_synth(wav: Any, numsamples: int, _events: Any) -> int:
            if numsamples > 0:
                chunks.append(
                    ctypes.string_at(
                        wav, numsamples * ctypes.sizeof(ctypes.c_short)
                    )
                )
            return 0

        espeak.SetSynthCallback(_on_synth)
        espeak.Synth(clean.encode("utf-8"), flags=espeak.ENDPAUSE | espeak.CHARS_UTF8)
        espeak.Synchronize()
        pcm = b"".join(chunks)
        if not pcm:
            raise RuntimeError("espeak produced no audio")

    return _pcm16_to_wav(pcm, _ESPEAK_SAMPLE_RATE_HZ), voice_name


def _speak_espeak_local(text: str, rate: int, volume: float) -> str:
    """Play speech on the server host via espeak-ng. Returns voice identifier."""
    wav, voice_name = _synthesize_espeak_wav_bytes(text, rate=rate, volume=volume)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(wav)
        path = tmp.name
    try:
        if sys.platform == "darwin":
            subprocess.run(["afplay", path], check=True, timeout=60)
        else:
            player = shutil.which("aplay") or shutil.which("paplay")
            if not player:
                raise RuntimeError("No audio player found (aplay/paplay).")
            subprocess.run([player, "-q", path], check=True, timeout=60)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return voice_name


def _synthesize_windows_sapi_wav_bytes(
    text: str, rate: int = 135, volume: float = 0.75
) -> tuple[bytes, str]:
    """Windows SAPI via PowerShell → mono/stereo PCM WAV bytes. Returns (wav, voice_name)."""
    # PowerShell treats curly quotes (U+2018/2019/201B) as string-quote chars,
    # so an LLM "Here’s" would terminate the single-quoted Speak() argument.
    # Normalize them to plain quotes BEFORE doubling the single quotes.
    escaped = _normalize_tts_text(text).replace("'", "''")
    sapi_rate = _sapi_rate_value(rate)
    vol = int(max(0.0, min(1.0, volume)) * 100)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        wpath = tmp.name
    wpath_ps = wpath.replace("'", "''")
    script = f"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$voice = $s.GetInstalledVoices() |
  Where-Object {{
    $_.VoiceInfo.Gender -eq 'Female' -or
    $_.VoiceInfo.Name -match 'Zira|Hazel|Heera|Susan'
  }} |
  Select-Object -First 1
if ($voice) {{
  $s.SelectVoice($voice.VoiceInfo.Name)
  Write-Output $voice.VoiceInfo.Name
}}
$s.Rate = {sapi_rate}
$s.Volume = {vol}
$s.SetOutputToWaveFile('{wpath_ps}')
$s.Speak('{escaped}')
$s.Dispose()
"""
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or "WAV synthesis failed")
        voice_line = (completed.stdout or "").strip().splitlines()
        voice_name = voice_line[0].strip() if voice_line else ""
        return open(wpath, "rb").read(), voice_name
    finally:
        try:
            os.unlink(wpath)
        except OSError:
            pass


def synthesize_sapi_wav_bytes(
    text: str, rate: int = 135, volume: float = 0.75
) -> tuple[bytes, str]:
    """Synthesize speech to WAV bytes with Piper/espeak as offline fallbacks."""
    _set_tts_synthesis_info("", "")
    provider = _tts_provider()
    if provider == "elevenlabs":
        if _elevenlabs_fallback_active():
            logger.info("ElevenLabs TTS fallback cooldown active; using local fallback.")
            wav, voice, used_provider = _synthesize_local_fallback_wav_bytes(
                text, rate, volume
            )
            _set_tts_synthesis_info(used_provider, voice)
            return wav, voice
        try:
            wav, voice = _synthesize_elevenlabs_wav_bytes(
                text, rate=rate, volume=volume
            )
            _set_tts_synthesis_info("elevenlabs", voice)
            return wav, voice
        except Exception as exc:
            _mark_elevenlabs_unavailable()
            logger.warning("ElevenLabs TTS failed (%s); falling back to Piper/local TTS.", exc)
            wav, voice, used_provider = _synthesize_local_fallback_wav_bytes(
                text, rate, volume
            )
            _set_tts_synthesis_info(used_provider, voice)
            return wav, voice
    if provider == "piper":
        try:
            wav, voice = _synthesize_piper_wav_bytes(
                text, rate=rate, volume=volume
            )
            _set_tts_synthesis_info("piper", voice)
            return wav, voice
        except Exception as exc:
            logger.warning("Piper TTS failed (%s); falling back to system TTS.", exc)
            if _use_windows_sapi():
                wav, voice = _synthesize_windows_sapi_wav_bytes(
                    text, rate=rate, volume=volume
                )
                _set_tts_synthesis_info("sapi", voice)
                return wav, voice
            wav, voice = _synthesize_espeak_wav_bytes(text, rate=rate, volume=volume)
            _set_tts_synthesis_info("local", voice)
            return wav, voice
    if provider == "sapi":
        wav, voice = _synthesize_windows_sapi_wav_bytes(
            text, rate=rate, volume=volume
        )
        _set_tts_synthesis_info("sapi", voice)
        return wav, voice
    wav, voice = _synthesize_espeak_wav_bytes(text, rate=rate, volume=volume)
    _set_tts_synthesis_info("local", voice)
    return wav, voice


def tts_status() -> dict[str, Any]:
    provider = _tts_provider()
    model_path = _piper_model_path().resolve()
    with _PIPER_LOCK:
        piper_preloaded = (
            _PIPER_VOICE is not None and _PIPER_VOICE_MODEL_PATH == model_path
        )
    out: dict[str, Any] = {
        "provider": provider,
        "elevenlabs_configured": bool(_elevenlabs_api_key()),
        "elevenlabs_fallback_active": _elevenlabs_fallback_active(),
        "elevenlabs_fallback_cooldown_seconds": _elevenlabs_fallback_cooldown_seconds(),
        "piper_available": _piper_available(),
        "piper_model_path": str(model_path),
        "piper_preloaded": piper_preloaded,
        "piper_length_scale": _piper_length_scale(),
    }
    if provider == "elevenlabs":
        out.update(
            {
                "voice_id": os.environ.get(
                    "ELEVENLABS_TTS_VOICE_ID", DEFAULT_ELEVENLABS_TTS_VOICE_ID
                ),
                "model": os.environ.get(
                    "ELEVENLABS_TTS_MODEL", DEFAULT_ELEVENLABS_TTS_MODEL
                ),
                "output_format": os.environ.get(
                    "ELEVENLABS_TTS_OUTPUT_FORMAT",
                    DEFAULT_ELEVENLABS_TTS_OUTPUT_FORMAT,
                ),
                "voice_settings": _elevenlabs_voice_settings(),
            }
        )
    elif provider == "piper":
        out["piper_voice"] = _piper_model_path().stem
    else:
        out.update(
            {
                "local_voice": os.environ.get("LOCAL_TTS_VOICE", "en+f3"),
                "local_rate": _espeak_rate_value(135),
            }
        )
    return out


def _face_greeting_db_probability() -> float:
    """Chance a vision greeting includes yesterday DB summary (0–1)."""
    raw = os.environ.get("FACE_GREETING_DB_PROBABILITY", "0.5").strip()
    try:
        return max(0.0, min(1.0, float(raw)))
    except ValueError:
        return 0.5


def _pick_include_db_context() -> bool:
    """Exclusive modes: plain greeting XOR greeting + DB context."""
    return random.random() < _face_greeting_db_probability()


@dataclass(frozen=True)
class _SpeechJob:
    """TTS queue item: LLM greeting (Ollama → SAPI / ESP)."""

    llm_name: str
    llm_return_visitor: bool = False
    #: If True, a successful job updates vision session state (first greet / last_spoken).
    track_vision_session: bool = True
    is_startup_greeting: bool = False
    #: True → weave yesterday summary; False → warm greeting only (never both).
    include_db_context: bool = False


class TTSService:
    def __init__(
        self,
        cooldown_seconds: float = 20.0,
        face_hold_seconds: float = 5.0,
        face_greeting_interval_seconds: float = 600.0,
        rate: int = 135,
        volume: float = 0.80,
        ollama_url: str = "",
        ollama_model: str = "",
    ) -> None:
        self.cooldown_seconds = cooldown_seconds
        self.face_hold_seconds = face_hold_seconds
        # Min seconds between vision "welcome back" for the same person (first greet ignores this).
        self.face_greeting_interval_seconds = max(1.0, float(face_greeting_interval_seconds))
        self.rate = rate
        self.volume = volume
        self._last_spoken_at: dict[str, float] = {}
        self._active_mode = ""
        self._active_name = ""
        self._last_face_seen_at = 0.0
        self._known_seen_once: set[str] = set()
        self._startup_greeted: set[str] = set()
        self._present_known_names: set[str] = set()
        self._vision_queued: set[str] = set()
        self._pending_jobs: list[_SpeechJob] = []
        self._suppress_vision_until: float = 0.0
        self._voice_cooldown_seconds: float = float(
            os.environ.get("VISION_GREETING_AFTER_VOICE_SECONDS", "90")
        )
        self._ollama_url = (ollama_url or "").strip()
        self._ollama_model = (ollama_model or "").strip()
        self._enabled = True
        self._last_error = ""
        self._voice_name = ""
        self._is_speaking = False
        self._spoken_count = 0
        self._last_spoken_text = ""
        self._lock = threading.Lock()
        self._worker = threading.Thread(target=self._run, name="tts-worker", daemon=True)
        self._on_summary_greeting_spoken: Callable[[str], None] | None = None
        self._worker.start()

    def set_on_summary_greeting_spoken(
        self, callback: Callable[[str], None] | None
    ) -> None:
        self._on_summary_greeting_spoken = callback

    def configure_llm(
        self,
        *,
        ollama_url: str | None = None,
        ollama_model: str | None = None,
    ) -> None:
        with self._lock:
            if ollama_url is not None:
                self._ollama_url = ollama_url.strip()
            if ollama_model is not None:
                self._ollama_model = ollama_model.strip()

    def _ollama_configured(self) -> bool:
        return bool((self._ollama_url or os.environ.get("OLLAMA_URL", "")).strip())

    def stop(self) -> None:
        with self._lock:
            self._enabled = False
            self._active_mode = ""
            self._active_name = ""
            self._last_face_seen_at = 0.0
            self._present_known_names.clear()
            self._known_seen_once.clear()
            self._startup_greeted.clear()
            self._last_spoken_at.clear()
            self._vision_queued.clear()
            self._pending_jobs.clear()
            self._suppress_vision_until = 0.0

    def is_speaking(self) -> bool:
        """True while any vision TTS job is running or queued."""
        with self._lock:
            return self._is_speaking or bool(self._pending_jobs)

    def needs_startup_summary_greeting(self, person_name: str) -> bool:
        """True until this person receives their first summary greeting this session."""
        name = str(person_name or "").strip()
        if not name or name.lower() in {"unknown", "face"}:
            return False
        with self._lock:
            return name not in self._startup_greeted

    def speak_to_esp(self, text: str, *, eye_expression: str | None = None) -> None:
        """Synthesize and POST WAV to ESP (vision empathy / greetings)."""
        self._output_speech(text, eye_expression=eye_expression)

    def notify_voice_interaction(self, viewer_name: str | None) -> None:
        """After a voice reply: drop stale vision greetings and pause auto-welcome."""
        with self._lock:
            now = time.time()
            self._suppress_vision_until = now + self._voice_cooldown_seconds
            # Drop queued vision greets (e.g. wrong person detected in background).
            self._pending_jobs.clear()
            self._vision_queued.clear()
            if viewer_name:
                cleaned = str(viewer_name).strip()
                if cleaned and cleaned.lower() not in {"unknown", "face"}:
                    self._known_seen_once.add(cleaned)
                    # Do not mark _startup_greeted here — voice should not skip the
                    # one-time yesterday-summary greeting for this server session.
                    self._last_spoken_at[cleaned] = now
                    self._active_name = cleaned
                    self._active_mode = "known"
                    self._last_face_seen_at = now

    def update_face_state(
        self, recognized_names: list[str], *, primary_name: str | None = None
    ) -> None:
        """Drive vision TTS for the **primary** face in frame only (largest / closest).

        Background or secondary recognized faces do not get auto-greetings.
        """
        with self._lock:
            now = time.time()
            names = []
            for n in recognized_names:
                name = str(n).strip()
                if not name:
                    continue
                lowered = name.lower()
                if lowered in {"unknown", "face"}:
                    continue
                names.append(name)
            names = list(dict.fromkeys(names))

            if not names:
                # Hold presence across brief gaps with no recognized face in frame.
                if (
                    self._last_face_seen_at > 0.0
                    and (now - self._last_face_seen_at) <= self.face_hold_seconds
                ):
                    return
                self._active_mode = ""
                self._active_name = ""
                self._present_known_names.clear()
                self._last_face_seen_at = 0.0
                return

            self._last_face_seen_at = now

            primary = str(primary_name or "").strip()
            if not primary or primary not in names:
                primary = names[0]

            prev_present = set(self._present_known_names)
            current_known = set(names)
            primary_re_entered = primary not in prev_present
            self._present_known_names = current_known

            if now >= self._suppress_vision_until:
                # First sight after boot: speak Phase C "Yesterday we discussed…" greeting.
                # Regular welcome-back is used only after that startup greeting has run.
                if primary not in self._startup_greeted:
                    self._enqueue_startup_greeting_locked(primary, now)
                else:
                    seen_before = primary in self._known_seen_once
                    if not seen_before:
                        self._enqueue_known_greeting_locked(
                            primary, now, welcome_back=False
                        )
                    elif primary_re_entered:
                        self._enqueue_known_greeting_locked(
                            primary, now, welcome_back=True
                        )

            self._active_mode = "known"
            self._active_name = primary

    def current_viewer_name(self) -> str | None:
        """Recognized person recently in frame (for voice personalization)."""
        with self._lock:
            name = str(self._active_name or "").strip()
            if not name or name.lower() in {"unknown", "face"}:
                return None
            if self._last_face_seen_at <= 0.0:
                return None
            if (time.time() - self._last_face_seen_at) > self.face_hold_seconds:
                return None
            return name

    def viewer_name_for_voice(self) -> str | None:
        """Short hold for voice follow-up — avoids ghost identities."""
        voice_hold = float(os.environ.get("FACE_VOICE_VIEWER_HOLD_SECONDS", "30"))
        with self._lock:
            name = str(self._active_name or "").strip()
            if not name or name.lower() in {"unknown", "face"}:
                return None
            if self._last_face_seen_at <= 0.0:
                return None
            if (time.time() - self._last_face_seen_at) > voice_hold:
                return None
            return name

    def greet(self, person_name: str) -> bool:
        if not self._enabled:
            return False

        name = person_name.strip()
        if not name or name.lower() in {"unknown", "face"}:
            return False

        now = time.time()
        with self._lock:
            if self._is_speaking:
                return False
            if name in self._vision_queued:
                return False
            last_spoken_at = self._last_spoken_at.get(name, 0.0)
            if now - last_spoken_at < self.cooldown_seconds:
                return False
            if not self._ollama_configured():
                self._last_error = "Ollama URL not set; cannot speak greeting."
                return False
            include_db = _pick_include_db_context()
            self._vision_queued.add(name)
            self._pending_jobs.append(
                _SpeechJob(
                    llm_name=name,
                    llm_return_visitor=False,
                    track_vision_session=False,
                    include_db_context=include_db,
                )
            )
        return True

    def status(self) -> dict[str, Any]:
        with self._lock:
            last_spoken = dict(self._last_spoken_at)
            active_mode = self._active_mode
            active_name = self._active_name
            spoken_count = self._spoken_count
            last_spoken_text = self._last_spoken_text
            pending_count = len(self._pending_jobs)

        return {
            "enabled": self._enabled,
            "cooldown_seconds": self.cooldown_seconds,
            "face_hold_seconds": self.face_hold_seconds,
            "face_greeting_interval_seconds": self.face_greeting_interval_seconds,
            "rate": self.rate,
            "volume": self.volume,
            "voice_name": self._voice_name,
            "is_speaking": self._is_speaking,
            "active_mode": active_mode,
            "active_name": active_name,
            "spoken_count": spoken_count,
            "last_spoken_text": last_spoken_text,
            "last_spoken": last_spoken,
            "pending_count": pending_count,
            "last_error": self._last_error,
            "ollama_url_set": self._ollama_configured(),
            "esp_play_wav_enabled": bool(os.environ.get("ESP_PLAY_WAV_URL", "").strip()),
            **tts_status(),
        }

    def _run(self) -> None:
        while True:
            with self._lock:
                if not self._enabled:
                    self._is_speaking = False
                    job: _SpeechJob | None = None
                else:
                    job = self._pending_jobs.pop(0) if self._pending_jobs else None

            if job is None:
                time.sleep(0.1)
                continue

            spoke_ok = False
            if job.is_startup_greeting:
                with self._lock:
                    self._is_speaking = True

            try:
                text = ""
                startup_parts = None
                greeting_error: Exception | None = None
                try:
                    from llm_service import greeting_for_face
                    from memory_service import get_memory_service

                    session_summary: str | None = None
                    if job.include_db_context:
                        try:
                            memory_svc = get_memory_service()
                            if memory_svc.ready:
                                session_summary = memory_svc.get_latest_summary_text(
                                    job.llm_name
                                )
                        except Exception:
                            session_summary = None

                    use_db = bool(job.include_db_context and (session_summary or "").strip())
                    logger.info(
                        "Vision greeting for %s (mode=%s, summary=%s)",
                        job.llm_name,
                        "db" if use_db else "plain",
                        "yes" if session_summary else "no",
                    )

                    if job.is_startup_greeting and use_db:
                        from llm_service import (
                            startup_greeting_from_summary,
                            startup_greeting_parts_from_summary,
                        )

                        logger.info(
                            "Startup summary greeting for %s (summary=%s)",
                            job.llm_name,
                            "yes" if session_summary else "no",
                        )
                        parts = startup_greeting_parts_from_summary(
                            job.llm_name,
                            session_summary or "",
                            model=self._ollama_model or None,
                            api_url=self._ollama_url or None,
                        )
                        startup_parts = parts
                        text = (
                            parts.spoken()
                            if parts is not None
                            else startup_greeting_from_summary(
                                job.llm_name,
                                session_summary or "",
                                model=self._ollama_model or None,
                                api_url=self._ollama_url or None,
                            )
                        )
                        logger.info(
                            "Startup summary greeting (LLM) for %s: %s",
                            job.llm_name,
                            text,
                        )
                    else:
                        # Plain greeting, or non-startup with optional DB summary.
                        text = greeting_for_face(
                            job.llm_name,
                            is_return_visitor=job.llm_return_visitor,
                            session_summary=session_summary if use_db else None,
                            is_startup_greeting=False,
                            model=self._ollama_model or None,
                            api_url=self._ollama_url or None,
                        )
                        if text:
                            text = _clamp_spoken_words(text)
                except Exception as exc:
                    greeting_error = exc
                    self._last_error = f"LLM greeting: {exc}"
                    text = ""
                    if not job.is_startup_greeting:
                        try:
                            from llm_service import brief_spoken_message

                            situation = (
                                f"You could not greet the recognized person {job.llm_name}."
                            )
                            text = brief_spoken_message(
                                situation,
                                model=self._ollama_model or None,
                                api_url=self._ollama_url or None,
                            )
                        except Exception as exc2:
                            self._last_error = f"LLM greeting: {exc}; recovery: {exc2}"
                            text = ""
                    else:
                        logger.warning(
                            "Startup summary greeting failed for %s: %s",
                            job.llm_name,
                            exc,
                        )

                if not text:
                    if job.is_startup_greeting:
                        text = f"Hello {job.llm_name}."
                        if greeting_error is not None:
                            logger.info(
                                "Startup summary greeting fallback for %s (LLM error: %s)",
                                job.llm_name,
                                greeting_error,
                            )
                        else:
                            logger.info(
                                "Startup summary greeting fallback for %s (empty LLM)",
                                job.llm_name,
                            )
                    else:
                        time.sleep(0.02)
                        continue
                else:
                    try:
                        with self._lock:
                            self._is_speaking = True
                        if job.is_startup_greeting and startup_parts is not None:
                            spoken_text = self._output_startup_greeting(startup_parts)
                        else:
                            spoken_text = self._output_speech(text)
                        spoke_ok = True
                        self._last_error = ""
                        with self._lock:
                            self._spoken_count += 1
                            self._last_spoken_text = spoken_text or text
                            self._last_spoken_at[job.llm_name] = time.time()
                            if job.track_vision_session and not job.llm_return_visitor:
                                self._known_seen_once.add(job.llm_name)
                        if job.is_startup_greeting:
                            logger.info(
                                "Startup summary greeting spoke for %s: %s",
                                job.llm_name,
                                (spoken_text or text)[:160],
                            )
                            cb = self._on_summary_greeting_spoken
                            if cb is not None:
                                try:
                                    cb(job.llm_name)
                                except Exception as exc:
                                    logger.warning(
                                        "Summary greeting callback failed: %s", exc
                                    )
                    except Exception as exc:
                        self._last_error = str(exc)
                        logger.warning(
                            "Startup summary greeting playback failed for %s: %s",
                            job.llm_name,
                            exc,
                        )
                    finally:
                        with self._lock:
                            self._is_speaking = False
            finally:
                with self._lock:
                    self._vision_queued.discard(job.llm_name)
                    if job.is_startup_greeting:
                        self._is_speaking = False
                        if spoke_ok:
                            self._startup_greeted.add(job.llm_name)
                            self._known_seen_once.add(job.llm_name)
                time.sleep(0.05)

    def _speak_once(self, text: str) -> None:
        try:
            self._output_speech(text)
            self._last_error = ""
        except Exception as exc:
            self._last_error = str(exc)

    def _esp_play_wav_url(self) -> str | None:
        from esp_playback import device_base_url

        base = device_base_url(getattr(self, "_playback_device_id", None))
        return f"{base}/play_wav" if base else None

    def set_playback_device_id(self, device_id: str | None) -> None:
        self._playback_device_id = (device_id or "").strip()

    def _synthesize_esp_wav(self, text: str, *, rate: int | None = None) -> bytes:
        wav, _ = synthesize_sapi_wav_bytes(
            text, rate=rate if rate is not None else self.rate, volume=self.volume
        )
        return resample_wav_bytes_to_mono_16bit(wav, ESP_PCM_SAMPLE_RATE_HZ)

    def _esp_wav_byte_size(self, text: str) -> int:
        return len(self._synthesize_esp_wav(text, rate=self.rate))

    def _chunk_text_for_esp(self, text: str) -> list[str]:
        """Split any spoken text into clips that fit ESP limit at normal TTS rate."""
        from esp_playback import ESP_MAX_PLAY_WAV_BYTES

        return chunk_text_for_esp_limit(
            text,
            self._esp_wav_byte_size,
            ESP_MAX_PLAY_WAV_BYTES,
        )

    def _play_esp_wav_chunks(
        self,
        chunks: list[str],
        *,
        eye_expression: str | None = None,
    ) -> str:
        """Queue WAV clips on ESP at normal TTS rate (back-to-back playback)."""
        from esp_playback import ESP_MAX_PLAY_WAV_BYTES, deliver_wav_to_device

        spoken: list[str] = []
        valid = [c.strip() for c in chunks if c.strip()]
        total = len(valid)
        device_id = getattr(self, "_playback_device_id", None)
        if not device_id:
            from device_registry import get_device_registry

            device_id = get_device_registry().ui_device_id()
        for i, chunk in enumerate(valid):
            wav = self._synthesize_esp_wav(chunk, rate=self.rate)
            if len(wav) > ESP_MAX_PLAY_WAV_BYTES:
                raise RuntimeError(
                    f"WAV chunk too large at rate {self.rate} "
                    f"({len(wav)} bytes; max {ESP_MAX_PLAY_WAV_BYTES}): {chunk[:100]}"
                )
            logger.info(
                "ESP play_wav %d/%d device=%s (%d bytes, rate=%d): %s",
                i + 1,
                total,
                device_id,
                len(wav),
                self.rate,
                chunk,
            )
            deliver_wav_to_device(
                device_id,
                wav,
                eye_expression=eye_expression if i == total - 1 else None,
            )
            spoken.append(chunk)
        return " ".join(spoken)

    def _play_esp_text(
        self,
        text: str,
        *,
        eye_expression: str | None = None,
    ) -> str:
        """Play arbitrary text on ESP — auto-chunked by measured WAV size."""
        text = _normalize_tts_text(text)
        if not text:
            return text
        chunks = self._chunk_text_for_esp(text)
        if len(chunks) == 1:
            logger.info(
                "ESP play_wav (1 clip, rate=%d): %s",
                self.rate,
                chunks[0],
            )
        else:
            logger.info(
                "ESP play_wav (%d clips, rate=%d): %s",
                len(chunks),
                self.rate,
                text[:120],
            )
        return self._play_esp_wav_chunks(chunks, eye_expression=eye_expression)

    def _output_startup_greeting(self, parts: Any) -> str:
        text = parts.spoken()
        if self._esp_play_wav_url():
            return self._play_esp_text(text)
        self._speak_with_windows_sapi(text)
        return text

    def _output_speech(
        self, text: str, *, eye_expression: str | None = None
    ) -> str:
        if self._esp_play_wav_url():
            return self._play_esp_text(text, eye_expression=eye_expression)
        self._speak_with_windows_sapi(text)
        return text

    def _synthesize_wav_windows_sapi(self, text: str) -> bytes:
        wav, voice_name = synthesize_sapi_wav_bytes(text, self.rate, self.volume)
        if voice_name:
            self._voice_name = voice_name
        return wav

    def _enqueue_startup_greeting_locked(self, name: str, now: float) -> None:
        """First sight after server boot — template greeting from Phase C summary."""
        if now < self._suppress_vision_until:
            return
        if name in self._vision_queued or name in self._startup_greeted:
            return
        if not self._esp_play_wav_url() and not self._ollama_configured():
            self._last_error = "Neither ESP playback nor Ollama is configured."
            self._startup_greeted.add(name)
            return

        include_db = _pick_include_db_context()
        self._vision_queued.add(name)
        logger.info(
            "Queued startup greeting for %s (mode=%s)",
            name,
            "db" if include_db else "plain",
        )
        self._pending_jobs.append(
            _SpeechJob(
                llm_name=name,
                llm_return_visitor=False,
                track_vision_session=True,
                is_startup_greeting=True,
                include_db_context=include_db,
            )
        )

    def _enqueue_known_greeting_locked(
        self, name: str, now: float, *, welcome_back: bool
    ) -> None:
        if now < self._suppress_vision_until:
            return
        if name in self._vision_queued:
            return
        if not self._ollama_configured():
            self._last_error = "Ollama URL not set; skipping face greeting."
            return

        last_spoken_at = self._last_spoken_at.get(name, 0.0)
        if welcome_back:
            if last_spoken_at <= 0.0:
                return
            if (now - last_spoken_at) < self.face_greeting_interval_seconds:
                return
        else:
            if last_spoken_at > 0.0 and (now - last_spoken_at) < 0.75:
                return

        include_db = _pick_include_db_context()
        self._vision_queued.add(name)
        logger.info(
            "Queued known greeting for %s (welcome_back=%s, mode=%s)",
            name,
            welcome_back,
            "db" if include_db else "plain",
        )
        self._pending_jobs.append(
            _SpeechJob(
                llm_name=name,
                llm_return_visitor=welcome_back,
                track_vision_session=True,
                include_db_context=include_db,
            )
        )

    def _speak_with_windows_sapi(self, text: str) -> None:
        if not _use_windows_sapi():
            voice_name = _speak_espeak_local(text, self.rate, self.volume)
            if voice_name:
                self._voice_name = voice_name
            return

        escaped_text = _normalize_tts_text(text).replace("'", "''")
        rate = _sapi_rate_value(self.rate)
        volume = int(max(0.0, min(1.0, self.volume)) * 100)
        script = f"""
Add-Type -AssemblyName System.Speech
$speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer
$voice = $speaker.GetInstalledVoices() |
  Where-Object {{
    $_.VoiceInfo.Gender -eq 'Female' -or
    $_.VoiceInfo.Name -match 'Zira|Hazel|Heera|Susan'
  }} |
  Select-Object -First 1
if ($voice) {{
  $speaker.SelectVoice($voice.VoiceInfo.Name)
}}
$speaker.Rate = {rate}
$speaker.Volume = {volume}
$speaker.Speak('{escaped_text}')
if ($voice) {{
  Write-Output $voice.VoiceInfo.Name
}}
"""
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or "Windows TTS failed")

        voice_name = completed.stdout.strip()
        if voice_name:
            self._voice_name = voice_name
