"""Split arbitrary spoken text into ESP /play_wav chunks under a byte limit.

Uses measured WAV sizes at the configured TTS rate — no per-user or per-phrase rules.
"""

from __future__ import annotations

import re
from typing import Callable

MeasureBytes = Callable[[str], int]


def split_spoken_sentences(text: str) -> list[str]:
    """Split on sentence boundaries while keeping terminal punctuation."""
    text = text.strip()
    if not text:
        return []
    parts = re.split(r"(?<=[.!?])\s+", text)
    return [part.strip() for part in parts if part.strip()]


class _WavSizer:
    """Cache WAV byte measurements and estimate words-per-chunk budget."""

    def __init__(self, measure: MeasureBytes, max_bytes: int) -> None:
        self._measure = measure
        self._max = max_bytes
        self._cache: dict[str, int] = {}
        self._bytes_per_word: float | None = None

    def size(self, text: str) -> int:
        key = text.strip()
        if not key:
            return 0
        if key not in self._cache:
            self._cache[key] = self._measure(key)
            words = len(key.split())
            if words > 0:
                bpw = self._cache[key] / words
                if self._bytes_per_word is None:
                    self._bytes_per_word = bpw
                else:
                    self._bytes_per_word = 0.65 * self._bytes_per_word + 0.35 * bpw
        return self._cache[key]

    def fits(self, text: str) -> bool:
        return 0 < len(text.strip()) and self.size(text) <= self._max

    def word_budget(self, remaining_words: int) -> int:
        if self._bytes_per_word is None or self._bytes_per_word <= 0:
            return max(1, min(remaining_words, 12))
        est = int(self._max / self._bytes_per_word * 0.90)
        return max(1, min(remaining_words, est))


def chunk_text_for_esp_limit(
    text: str,
    measure_wav_bytes: MeasureBytes,
    max_bytes: int,
) -> list[str]:
    """Return ordered text chunks, each producing WAV <= max_bytes at normal TTS rate."""
    text = text.strip()
    if not text:
        return []
    sizer = _WavSizer(measure_wav_bytes, max_bytes)
    if sizer.fits(text):
        return [text]
    return _chunk_sentences(text, sizer)


def _chunk_sentences(text: str, sizer: _WavSizer) -> list[str]:
    sentences = split_spoken_sentences(text)
    if len(sentences) <= 1:
        return _chunk_by_words(text, sizer)

    chunks: list[str] = []
    buf = ""
    for sentence in sentences:
        trial = f"{buf} {sentence}".strip() if buf else sentence
        if sizer.fits(trial):
            buf = trial
            continue
        if buf:
            chunks.append(buf)
            buf = ""
        if sizer.fits(sentence):
            buf = sentence
        else:
            chunks.extend(_chunk_by_words(sentence, sizer))
    if buf:
        chunks.append(buf)
    return chunks


def _chunk_by_words(text: str, sizer: _WavSizer) -> list[str]:
    words = text.split()
    if not words:
        return []
    chunks: list[str] = []
    index = 0
    while index < len(words):
        remaining = len(words) - index
        budget = sizer.word_budget(remaining)
        placed = False
        while budget >= 1:
            piece = " ".join(words[index : index + budget]).strip()
            if sizer.fits(piece):
                chunks.append(piece)
                index += budget
                placed = True
                break
            budget -= 1
        if not placed:
            # Last resort: one word (should be rare; upstream should shorten text).
            chunks.append(words[index])
            index += 1
    return chunks
