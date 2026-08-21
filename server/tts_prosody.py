"""Mood-based Piper delivery: infer style from spoken text, then retune speed/pitch/volume."""

from __future__ import annotations

import io
import logging
import os
import re
import shutil
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

SPEECH_STYLES: tuple[str, ...] = (
    "greeting",
    "happy",
    "sad",
    "tired",
    "curious",
    "surprised",
    "recalling",
    "neutral",
)

_GREETING_OPENER_RE = re.compile(
    r"^\s*(?:oh\s+)?(?:hello|hiya|hi\s+there|hi|hey\s+there|hey|"
    r"good\s+(?:morning|afternoon|evening|day)|welcome)\b",
    re.I,
)
_GREETING_BODY_RE = re.compile(
    r"\b(?:"
    r"(?:nice|good|lovely|great|wonderful|so\s+good)\s+to\s+(?:see|meet|have)\s+you"
    r"|welcome\s+back"
    r"|how\s+are\s+you"
    r"|how(?:'s|s)?\s+it\s+going"
    r"|good\s+to\s+(?:see|have)\s+you"
    r"|what\s+a\s+(?:nice|lovely)\s+surprise"
    r")\b",
    re.I,
)
_SAD_OVERRIDE_RE = re.compile(
    r"\b(?:"
    r"sorry\s+to\s+hear|unfortunately|sadly|"
    r"i(?:'m| am) sorry|feel(?:ing)?\s+(?:sad|down|low|awful|terrible)|"
    r"passed\s+away|not\s+feeling\s+well|rough\s+day|bad\s+day|"
    r"that(?:'s| is)\s+(?:hard|tough|awful)|here\s+if\s+you\s+need"
    r")\b",
    re.I,
)
_TIRED_OVERRIDE_RE = re.compile(
    r"\b(?:"
    r"feel(?:ing)?\s+(?:tired|sleepy|exhausted)|so\s+tired|"
    r"get\s+some\s+(?:rest|sleep)|time\s+(?:to\s+sleep|for\s+bed)|"
    r"good\s+night|you\s+(?:sound|look)\s+tired|long\s+day|"
    r"need\s+(?:a\s+break|rest|sleep)|worn\s+out"
    r")\b",
    re.I,
)


@dataclass(frozen=True)
class SpeechProsody:
    """Piper knobs plus a small post-synth pitch shift."""

    style: str
    length_mul: float
    noise_scale: float
    noise_w_scale: float
    volume_mul: float
    pitch_semitones: float


_STYLE_PRESETS: dict[str, SpeechProsody] = {
    "greeting": SpeechProsody(
        "greeting",
        length_mul=0.78,
        noise_scale=1.05,
        noise_w_scale=1.15,
        volume_mul=1.22,
        pitch_semitones=3.2,
    ),
    "happy": SpeechProsody(
        "happy",
        length_mul=0.82,
        noise_scale=0.98,
        noise_w_scale=1.08,
        volume_mul=1.18,
        pitch_semitones=2.4,
    ),
    "sad": SpeechProsody(
        "sad",
        length_mul=1.34,
        noise_scale=0.26,
        noise_w_scale=0.32,
        volume_mul=0.55,
        pitch_semitones=-3.8,
    ),
    "tired": SpeechProsody(
        "tired",
        length_mul=1.42,
        noise_scale=0.22,
        noise_w_scale=0.28,
        volume_mul=0.58,
        pitch_semitones=-3.2,
    ),
    "curious": SpeechProsody(
        "curious",
        length_mul=1.02,
        noise_scale=0.72,
        noise_w_scale=0.85,
        volume_mul=1.00,
        pitch_semitones=0.5,
    ),
    "surprised": SpeechProsody(
        "surprised",
        length_mul=0.86,
        noise_scale=0.88,
        noise_w_scale=1.00,
        volume_mul=1.10,
        pitch_semitones=1.6,
    ),
    "recalling": SpeechProsody(
        "recalling",
        length_mul=1.10,
        noise_scale=0.50,
        noise_w_scale=0.58,
        volume_mul=0.92,
        pitch_semitones=-0.4,
    ),
    "neutral": SpeechProsody(
        "neutral",
        length_mul=1.00,
        noise_scale=0.667,
        noise_w_scale=0.80,
        volume_mul=1.00,
        pitch_semitones=0.0,
    ),
}


def piper_prosody_enabled() -> bool:
    raw = os.environ.get("PIPER_PROSODY", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def piper_pitch_offset() -> float:
    """Standing pitch shift on top of mood, in semitones.

    Default is 0 (natural pitch). The old +4 lift made 16 kHz Piper thin and
    harsh on the P4 speaker. Override with ``PIPER_PITCH_SEMITONES``.
    """
    raw = os.environ.get("PIPER_PITCH_SEMITONES", "0").strip()
    if not raw:
        return 0.0
    try:
        return max(-12.0, min(12.0, float(raw)))
    except ValueError:
        return 0.0


def piper_robotic_amount() -> float:
    """0 = warm natural timbre, 1 = strongest cheap-robot AM / thinning."""
    raw = os.environ.get("PIPER_ROBOTIC", "0").strip()
    if not raw:
        return 0.0
    try:
        return max(0.0, min(1.0, float(raw)))
    except ValueError:
        return 0.0


def infer_speech_style(text: str) -> str:
    """Pick a delivery style from the sentence Piper is about to speak."""
    clean = (text or "").strip()
    if not clean:
        return "neutral"
    if _TIRED_OVERRIDE_RE.search(clean):
        return "tired"
    if _SAD_OVERRIDE_RE.search(clean):
        return "sad"
    if _is_greeting(clean):
        return "greeting"
    try:
        from eye_expression import infer_eye_expression

        tag = infer_eye_expression(clean, reply_path="llm")
        if tag in _STYLE_PRESETS and tag != "neutral":
            return tag
    except Exception:
        pass
    if clean.endswith("!") and len(clean.split()) <= 16:
        return "happy"
    if clean.endswith("?"):
        return "curious"
    return "neutral"


def prosody_for_style(style: str) -> SpeechProsody:
    return _STYLE_PRESETS.get(style, _STYLE_PRESETS["neutral"])


def infer_speech_prosody(text: str) -> SpeechProsody:
    if not piper_prosody_enabled():
        return _STYLE_PRESETS["neutral"]
    return prosody_for_style(infer_speech_style(text))


def piper_pitch_backend() -> str:
    """Pitch post-process backend for Piper: ``ffmpeg`` (default) or ``numpy``."""
    raw = os.environ.get("PIPER_PITCH_BACKEND", "ffmpeg").strip().lower()
    return raw if raw in {"ffmpeg", "numpy"} else "ffmpeg"


def pitch_shift_wav_bytes_ffmpeg(wav_bytes: bytes, semitones: float) -> bytes:
    """Shift pitch via ffmpeg ``asetrate`` + ``atempo`` while preserving duration."""
    if not wav_bytes or abs(semitones) < 0.05:
        return wav_bytes
    binary = shutil.which(os.environ.get("FFMPEG_BINARY", "ffmpeg"))
    if not binary:
        raise RuntimeError("ffmpeg is not installed")

    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        sample_rate = wf.getframerate()
        if wf.getsampwidth() != 2 or wf.getnchannels() < 1:
            raise RuntimeError("ffmpeg pitch shift requires 16-bit PCM WAV")

    factor = 2.0 ** (semitones / 12.0)
    new_rate = max(1, int(round(sample_rate * factor)))
    tempo = 1.0 / factor
    audio_filter = (
        f"asetrate={new_rate},atempo={tempo:.6f},aresample={sample_rate}"
    )
    in_path = out_path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as inp:
            inp.write(wav_bytes)
            in_path = inp.name
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as out:
            out_path = out.name
        completed = subprocess.run(
            [
                binary,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                in_path,
                "-af",
                audio_filter,
                out_path,
            ],
            capture_output=True,
            check=False,
            timeout=60,
        )
        if completed.returncode != 0:
            err = (completed.stderr or b"").decode("utf-8", errors="replace").strip()
            raise RuntimeError(err or "ffmpeg pitch shift failed")
        return Path(out_path).read_bytes()
    finally:
        for path in (in_path, out_path):
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass


def pitch_shift_wav_bytes_preserve_tempo(wav_bytes: bytes, semitones: float) -> bytes:
    """Preserve duration when shifting pitch; prefer ffmpeg for Piper Amy."""
    if piper_pitch_backend() == "ffmpeg":
        try:
            return pitch_shift_wav_bytes_ffmpeg(wav_bytes, semitones)
        except Exception as exc:
            logger.warning(
                "ffmpeg pitch shift failed (%s); falling back to numpy.", exc
            )
    return pitch_shift_wav_bytes(wav_bytes, semitones)


def pitch_shift_wav_bytes(wav_bytes: bytes, semitones: float) -> bytes:
    """Shift pitch while keeping duration and sample rate. No-op for tiny shifts."""
    if not wav_bytes or abs(semitones) < 0.05:
        return wav_bytes
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        sample_rate = wf.getframerate()
        frames = wf.readframes(wf.getnframes())
    if sample_width != 2 or channels < 1 or not frames:
        return wav_bytes

    pcm = np.frombuffer(frames, dtype=np.int16).astype(np.float32)
    if channels > 1:
        pcm = pcm.reshape(-1, channels)
    else:
        pcm = pcm.reshape(-1, 1)

    factor = 2.0 ** (semitones / 12.0)
    n_src = pcm.shape[0]
    n_pitched = max(1, int(round(n_src / factor)))
    x_src = np.linspace(0.0, 1.0, num=n_src, endpoint=False)
    x_pitched = np.linspace(0.0, 1.0, num=n_pitched, endpoint=False)
    x_out = np.linspace(0.0, 1.0, num=n_src, endpoint=False)
    shifted = np.empty((n_src, channels), dtype=np.float32)
    for ch in range(channels):
        pitched = np.interp(x_pitched, x_src, pcm[:, ch])
        shifted[:, ch] = np.interp(x_out, x_pitched, pitched)

    return _pcm16_to_wav(shifted, channels, sample_rate)


def robotic_color_wav_bytes(wav_bytes: bytes, amount: float = 0.75) -> bytes:
    """Thin the chest and add a light AM carrier so Piper sounds more mechanical."""
    amount = max(0.0, min(1.0, float(amount)))
    if not wav_bytes or amount < 0.05:
        return wav_bytes
    pcm, channels, sample_rate = _wav_to_pcm16(wav_bytes)
    if pcm is None:
        return wav_bytes

    n = pcm.shape[0]
    t = np.arange(n, dtype=np.float32) / float(sample_rate)
    prev = np.vstack((pcm[:1], pcm[:-1]))
    hp = pcm - prev
    thinned = (1.0 - 0.55 * amount) * pcm + (0.55 * amount) * hp

    # Soft square-ish AM around 58 Hz — classic toy-robot buzz without crushing words.
    carrier = 0.58 + 0.42 * np.sign(np.sin(2.0 * np.pi * 58.0 * t))
    carrier = carrier.reshape(-1, 1)
    buzzed = thinned * ((1.0 - 0.42 * amount) + (0.42 * amount) * carrier)

    delay = max(1, int(round(0.004 * sample_rate)))
    echo = np.zeros_like(buzzed)
    echo[delay:] = buzzed[:-delay]
    mixed = buzzed + (0.22 * amount) * echo
    peak = float(np.max(np.abs(mixed))) if mixed.size else 0.0
    if peak > 32000.0:
        mixed *= 32000.0 / peak
    return _pcm16_to_wav(mixed, channels, sample_rate)


def _wav_to_pcm16(wav_bytes: bytes) -> tuple[np.ndarray | None, int, int]:
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        sample_rate = wf.getframerate()
        frames = wf.readframes(wf.getnframes())
    if sample_width != 2 or channels < 1 or not frames:
        return None, 0, 0
    pcm = np.frombuffer(frames, dtype=np.int16).astype(np.float32)
    if channels > 1:
        pcm = pcm.reshape(-1, channels)
    else:
        pcm = pcm.reshape(-1, 1)
    return pcm, channels, sample_rate


def _pcm16_to_wav(pcm: np.ndarray, channels: int, sample_rate: int) -> bytes:
    out_i16 = np.clip(pcm, -32768.0, 32767.0).astype(np.int16)
    output = io.BytesIO()
    with wave.open(output, "wb") as wo:
        wo.setnchannels(channels)
        wo.setsampwidth(2)
        wo.setframerate(sample_rate)
        wo.writeframes(out_i16.tobytes())
    return output.getvalue()


def _is_greeting(text: str) -> bool:
    words = text.split()
    if _GREETING_OPENER_RE.search(text) and len(words) <= 24:
        return True
    if _GREETING_BODY_RE.search(text) and len(words) <= 28:
        return True
    return False
