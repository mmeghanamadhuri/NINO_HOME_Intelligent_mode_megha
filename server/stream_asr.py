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
DEFAULT_SPEECH_MS = 160
DEFAULT_SILENCE_MS = 700
DEFAULT_MAX_MS = 15000
DEFAULT_MIN_SPEECH_MS = 200
SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2


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

    def reset(self) -> None:
        self.heard_speech = False
        self.speech_streak_ms = 0
        self.silence_ms_run = 0
        self.uttered_ms = 0
        self.speech_ms_total = 0
        self.ended = False

    def feed(self, pcm: bytes) -> str:
        """Feed one PCM frame. Returns idle, speech, end_of_speech, or timeout."""
        if self.ended:
            return "end_of_speech"
        energy = pcm_frame_energy(pcm)
        self.uttered_ms += self.frame_ms
        if energy >= self.start_energy:
            self.speech_streak_ms += self.frame_ms
            self.silence_ms_run = 0
            self.speech_ms_total += self.frame_ms
            if self.speech_streak_ms >= self.speech_ms:
                self.heard_speech = True
        else:
            self.speech_streak_ms = 0
            if self.heard_speech and energy < self.quiet_energy:
                self.silence_ms_run += self.frame_ms
            elif self.heard_speech:
                self.silence_ms_run = 0

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


def stream_vad_from_environ() -> StreamEndOfSpeech:
    quiet = _env_int("ASR_EOS_QUIET_ENERGY", DEFAULT_QUIET_ENERGY)
    start = _env_int("ASR_EOS_START_ENERGY", DEFAULT_START_ENERGY)
    if quiet >= start:
        quiet = max(1, start - 1)
    return StreamEndOfSpeech(
        start_energy=start,
        quiet_energy=quiet,
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

    def feed(self, chunk: bytes) -> str:
        if chunk:
            self.pcm.extend(chunk)
        return self.vad.feed(chunk or b"")
