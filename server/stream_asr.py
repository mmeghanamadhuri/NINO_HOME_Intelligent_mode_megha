"""Server-side end-of-speech for streamed 16 kHz mono PCM.

The P4 sends Aux-in frames continuously. This module decides when an
utterance is complete so ASR can run and further audio can be ignored
until TTS finishes.
"""

from __future__ import annotations

import array
import os
from dataclasses import dataclass, field

DEFAULT_FRAME_MS = 20
DEFAULT_START_ENERGY = 50
DEFAULT_QUIET_ENERGY = 20
# Once speech started, only frames at/above this reset the silence hangover.
DEFAULT_CONTINUE_ENERGY = 50
DEFAULT_SPEECH_MS = 160
DEFAULT_SILENCE_MS = 450
DEFAULT_MAX_MS = 30000
# Registration yes/no/name/spell/confirm: 60s of no speech → guest, not goodbye.
DEFAULT_REGISTER_MAX_MS = 60000
DEFAULT_MIN_SPEECH_MS = 200
# Quiet Aux can drop the start bar, but not into electrical-tick range.
ADAPTIVE_START_MIN = 6
SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2
# P4 stream frames are 20 ms / 640 bytes. Allow a little jitter either side.
STREAM_PCM_FRAME_MIN = 320
STREAM_PCM_FRAME_MAX = 2048


def looks_like_stream_pcm_frame(data: bytes | None) -> bool:
    """True for a short raw PCM websocket frame, not a complete WAV clip."""
    if not data:
        return False
    n = len(data)
    if n < STREAM_PCM_FRAME_MIN or n > STREAM_PCM_FRAME_MAX or n % 2:
        return False
    if n >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return False
    return True


def pcm_frame_energy(pcm: bytes) -> int:
    """Mean absolute 16-bit sample energy for one PCM frame."""
    if not pcm or len(pcm) < 2:
        return 0
    if len(pcm) % 2:
        pcm = pcm[:-1]
    samples = array.array("h")
    samples.frombytes(pcm)
    if not samples:
        return 0
    total = 0
    for sample in samples:
        total += -sample if sample < 0 else sample
    return total // len(samples)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


@dataclass
class StreamEndOfSpeech:
    """Energy VAD over 20 ms PCM frames. Returns end_of_speech after silence."""

    start_energy: int = DEFAULT_START_ENERGY
    quiet_energy: int = DEFAULT_QUIET_ENERGY
    continue_energy: int = DEFAULT_CONTINUE_ENERGY
    speech_ms: int = DEFAULT_SPEECH_MS
    silence_ms: int = DEFAULT_SILENCE_MS
    max_ms: int = DEFAULT_MAX_MS
    min_speech_ms: int = DEFAULT_MIN_SPEECH_MS
    frame_ms: int = DEFAULT_FRAME_MS
    heard_speech: bool = False
    speech_streak_ms: int = 0
    silence_ms_run: int = 0
    uttered_ms: int = 0
    speech_ms_total: int = 0
    ended: bool = False
    last_energy: int = 0
    peak_energy: int = 0
    noise_sum: int = 0
    noise_n: int = 0

    def reset(self) -> None:
        self.heard_speech = False
        self.speech_streak_ms = 0
        self.silence_ms_run = 0
        self.uttered_ms = 0
        self.speech_ms_total = 0
        self.ended = False
        self.last_energy = 0
        self.peak_energy = 0
        self.noise_sum = 0
        self.noise_n = 0

    def effective_start(self) -> int:
        """Absolute start, or a lower bar when Aux idle is clearly quieter."""
        if self.noise_n < 15:
            return self.start_energy
        noise_mean = self.noise_sum // self.noise_n
        adaptive = noise_mean * 3 + 8
        return max(ADAPTIVE_START_MIN, min(self.start_energy, adaptive))

    def feed(self, pcm: bytes) -> str:
        """Feed one PCM frame. Returns idle, speech, end_of_speech, or timeout."""
        if self.ended:
            return "end_of_speech"
        energy = pcm_frame_energy(pcm)
        self.last_energy = energy
        if energy > self.peak_energy:
            self.peak_energy = energy
        if energy < self.quiet_energy:
            self.noise_sum += energy
            self.noise_n += 1
        start = self.effective_start()
        continue_at = max(self.continue_energy, start + 1)
        self.uttered_ms += self.frame_ms
        if not self.heard_speech:
            if energy >= start:
                self.speech_streak_ms += self.frame_ms
                self.silence_ms_run = 0
                self.speech_ms_total += self.frame_ms
                if self.speech_streak_ms >= self.speech_ms:
                    self.heard_speech = True
            else:
                self.speech_streak_ms = 0
        elif energy >= continue_at:
            self.silence_ms_run = 0
            self.speech_ms_total += self.frame_ms
        else:
            self.silence_ms_run += self.frame_ms

        if (
            self.heard_speech
            and self.speech_ms_total >= self.min_speech_ms
            and self.silence_ms_run >= self.silence_ms
        ):
            self.ended = True
            return "end_of_speech"
        if self.uttered_ms >= self.max_ms:
            self.ended = True
            return "end_of_speech" if self.heard_speech else "timeout"
        return "speech" if self.heard_speech else "idle"


def stream_listen_max_ms(*, in_registration: bool) -> int:
    """VAD listen cap: 60s while registering, 30s idle-goodbye afterwards."""
    return DEFAULT_REGISTER_MAX_MS if in_registration else DEFAULT_MAX_MS


def stream_idle_timeout_ends_session(
    reason: str, *, in_registration: bool = False
) -> bool:
    """True when a listen turn hit max_ms with no speech — goodbye, not skip.

    During registration, the same timeout converts the user to a guest and
    keeps the session open.
    """
    return reason == "timeout" and not in_registration


def stream_vad_from_environ() -> StreamEndOfSpeech:
    quiet = _env_int("ASR_EOS_QUIET_ENERGY", DEFAULT_QUIET_ENERGY)
    start = _env_int("ASR_EOS_START_ENERGY", DEFAULT_START_ENERGY)
    if quiet >= start:
        quiet = max(1, start - 1)
    return StreamEndOfSpeech(
        start_energy=start,
        quiet_energy=quiet,
        continue_energy=_env_int(
            "ASR_EOS_CONTINUE_ENERGY", DEFAULT_CONTINUE_ENERGY
        ),
        speech_ms=_env_int("ASR_EOS_SPEECH_MS", DEFAULT_SPEECH_MS),
        silence_ms=_env_int("ASR_EOS_SILENCE_MS", DEFAULT_SILENCE_MS),
        max_ms=_env_int("ASR_EOS_MAX_MS", DEFAULT_MAX_MS),
        min_speech_ms=_env_int("ASR_EOS_MIN_SPEECH_MS", DEFAULT_MIN_SPEECH_MS),
    )


@dataclass
class UtteranceBuffer:
    pcm: bytearray = field(default_factory=bytearray)
    vad: StreamEndOfSpeech = field(default_factory=stream_vad_from_environ)

    def reset(self) -> None:
        self.pcm.clear()
        self.vad.reset()

    def set_listen_max_ms(self, max_ms: int) -> None:
        self.vad.max_ms = max(1, int(max_ms))

    def feed(self, chunk: bytes) -> str:
        if chunk:
            self.pcm.extend(chunk)
        return self.vad.feed(chunk or b"")
