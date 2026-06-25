from __future__ import annotations

import io
import logging
import os
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
from typing import Any

import requests

from wav_resample import ESP_PCM_SAMPLE_RATE_HZ, resample_wav_bytes_to_mono_16bit

logger = logging.getLogger(__name__)

DEFAULT_ELEVENLABS_TTS_VOICE_ID = "f1K8uOKtx0TAmtXBiLqx"
DEFAULT_ELEVENLABS_TTS_MODEL = "eleven_multilingual_v2"
DEFAULT_ELEVENLABS_TTS_OUTPUT_FORMAT = "pcm_16000"
DEFAULT_ELEVENLABS_TTS_SAMPLE_RATE_HZ = 16000

_ESPEAK_LOCK = threading.Lock()
_ESPEAK_READY = False
_ESPEAK_SAMPLE_RATE_HZ = 22050
# espeak-ng female variants (+f3 = soft British female). Use short names; gmw/en-gb+f3 fails.
_PREFERRED_ESPEAK_VOICES = (
    "en+f3",
    "en+f4",
    "en+f2",
    "en+f5",
    "en-gb+f4",
)


def _normalize_tts_text(text: str) -> str:
    return (
        text.replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201B", "'")
        .replace("\u201C", '"')
        .replace("\u201D", '"')
    )


def _use_windows_sapi() -> bool:
    return sys.platform == "win32" and shutil.which("powershell") is not None


def _elevenlabs_api_key() -> str:
    return os.environ.get("ELEVENLABS_API_KEY", "").strip()


def _tts_provider() -> str:
    provider = os.environ.get("TTS_PROVIDER", "").strip().lower()
    if provider in {"elevenlabs", "sapi", "local"}:
        return provider
    if _elevenlabs_api_key():
        return "elevenlabs"
    if _use_windows_sapi():
        return "sapi"
    return "local"


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
    """Synthesize speech to WAV bytes. Default: ElevenLabs when API key is set."""
    provider = _tts_provider()
    if provider == "elevenlabs":
        try:
            return _synthesize_elevenlabs_wav_bytes(text, rate=rate, volume=volume)
        except Exception as exc:
            logger.warning("ElevenLabs TTS failed (%s); falling back to local TTS.", exc)
            if _use_windows_sapi():
                return _synthesize_windows_sapi_wav_bytes(
                    text, rate=rate, volume=volume
                )
            return _synthesize_espeak_wav_bytes(text, rate=rate, volume=volume)
    if provider == "sapi":
        return _synthesize_windows_sapi_wav_bytes(text, rate=rate, volume=volume)
    return _synthesize_espeak_wav_bytes(text, rate=rate, volume=volume)


def tts_status() -> dict[str, Any]:
    provider = _tts_provider()
    out: dict[str, Any] = {
        "provider": provider,
        "elevenlabs_configured": bool(_elevenlabs_api_key()),
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
    else:
        out.update(
            {
                "local_voice": os.environ.get("LOCAL_TTS_VOICE", "en+f3"),
                "local_rate": _espeak_rate_value(135),
            }
        )
    return out


@dataclass(frozen=True)
class _SpeechJob:
    """TTS queue item: LLM greeting (Ollama → SAPI / ESP)."""

    llm_name: str
    llm_return_visitor: bool = False
    #: If True, a successful job updates vision session state (first greet / last_spoken).
    track_vision_session: bool = True


class TTSService:
    def __init__(
        self,
        cooldown_seconds: float = 20.0,
        face_hold_seconds: float = 5.0,
        face_greeting_interval_seconds: float = 600.0,
        rate: int = 135,
        volume: float = 0.75,
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
        self._worker.start()

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
            self._last_spoken_at.clear()
            self._vision_queued.clear()
            self._pending_jobs.clear()
            self._suppress_vision_until = 0.0

    def is_speaking(self) -> bool:
        """True while any vision TTS job is running or queued."""
        with self._lock:
            return self._is_speaking or bool(self._pending_jobs)

    def speak_to_esp(self, text: str, *, eye_expression: str | None = None) -> None:
        """Synthesize and POST WAV to ESP (vision empathy / greetings)."""
        from esp_playback import post_wav_to_esp

        wav = self._synthesize_wav_windows_sapi(text)
        wav = resample_wav_bytes_to_mono_16bit(wav, ESP_PCM_SAMPLE_RATE_HZ)
        post_wav_to_esp(wav, eye_expression=eye_expression)

    def notify_voice_interaction(self, viewer_name: str | None) -> None:
        """After a voice reply: drop stale vision greetings and pause auto-welcome."""
        from pipeline_priority import notify_voice_interaction as _notify_priority

        _notify_priority()
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

            vision_emotion_on = os.environ.get("VISION_EMOTION_ENABLED", "1").strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
            if now >= self._suppress_vision_until and not vision_emotion_on:
                seen_before = primary in self._known_seen_once
                if not seen_before:
                    self._enqueue_known_greeting_locked(primary, now, welcome_back=False)
                elif primary_re_entered:
                    self._enqueue_known_greeting_locked(primary, now, welcome_back=True)
            elif now >= self._suppress_vision_until and vision_emotion_on:
                self._known_seen_once.add(primary)

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
        """Longer hold than vision UI — keeps name for voice follow-up questions."""
        voice_hold = max(self.face_hold_seconds, 120.0)
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
            self._vision_queued.add(name)
            self._pending_jobs.append(
                _SpeechJob(
                    llm_name=name,
                    llm_return_visitor=False,
                    track_vision_session=False,
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

            try:
                text = ""
                try:
                    from llm_service import greeting_for_face

                    text = greeting_for_face(
                        job.llm_name,
                        is_return_visitor=job.llm_return_visitor,
                        model=self._ollama_model or None,
                        api_url=self._ollama_url or None,
                    )
                except Exception as exc:
                    self._last_error = f"LLM greeting: {exc}"
                    text = ""
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

                if not text:
                    time.sleep(0.02)
                else:
                    try:
                        with self._lock:
                            self._is_speaking = True
                        self._output_speech(text)
                        self._last_error = ""
                        with self._lock:
                            self._spoken_count += 1
                            self._last_spoken_text = text
                            self._last_spoken_at[job.llm_name] = time.time()
                            if job.track_vision_session and not job.llm_return_visitor:
                                self._known_seen_once.add(job.llm_name)
                    except Exception as exc:
                        self._last_error = str(exc)
                    finally:
                        with self._lock:
                            self._is_speaking = False
            finally:
                with self._lock:
                    self._vision_queued.discard(job.llm_name)
                time.sleep(0.05)

    def _speak_once(self, text: str) -> None:
        try:
            self._output_speech(text)
            self._last_error = ""
        except Exception as exc:
            self._last_error = str(exc)

    def _esp_play_wav_url(self) -> str | None:
        u = os.environ.get("ESP_PLAY_WAV_URL", "").strip()
        return u if u else None

    def _output_speech(self, text: str) -> None:
        if self._esp_play_wav_url():
            wav = self._synthesize_wav_windows_sapi(text)
            wav = resample_wav_bytes_to_mono_16bit(wav, ESP_PCM_SAMPLE_RATE_HZ)
            self._post_wav_to_esp(wav)
            return
        self._speak_with_windows_sapi(text)

    def _synthesize_wav_windows_sapi(self, text: str) -> bytes:
        wav, voice_name = synthesize_sapi_wav_bytes(text, self.rate, self.volume)
        if voice_name:
            self._voice_name = voice_name
        return wav

    def _post_wav_to_esp(self, wav: bytes) -> None:
        url = self._esp_play_wav_url()
        if not url:
            raise RuntimeError("ESP_PLAY_WAV_URL is not set")

        req = urllib.request.Request(
            url,
            data=wav,
            method="POST",
            headers={"Content-Type": "audio/wav"},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"ESP play_wav HTTP {resp.status}")
                body = resp.read()
                if b'"ok":true' not in body and b'"ok": true' not in body:
                    if b'"ok":false' in body or b'"ok": false' in body:
                        raise RuntimeError(body.decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
            raise RuntimeError(f"ESP HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"ESP URL error: {exc}") from exc

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

        self._vision_queued.add(name)
        self._pending_jobs.append(
            _SpeechJob(
                llm_name=name,
                llm_return_visitor=welcome_back,
                track_vision_session=True,
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
