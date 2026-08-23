"""Playback busy window keeps ASR muted until TTS finishes."""

from __future__ import annotations

import struct
import unittest

import esp_playback as playback


def _make_wav(seconds: float = 2.0, rate: int = 16000) -> bytes:
    samples = int(seconds * rate)
    pcm = b"\x00\x00" * samples
    data_size = len(pcm)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_size,
        b"WAVE",
        b"fmt ",
        16,
        1,
        1,
        rate,
        rate * 2,
        2,
        16,
        b"data",
        data_size,
    )
    return header + pcm


class PlaybackBusyTests(unittest.TestCase):
    def tearDown(self) -> None:
        playback.clear_device_busy("dev-a")

    def test_split_wav_for_esp_under_limit(self) -> None:
        wav = _make_wav(1.0)
        chunks = playback.split_wav_for_esp(wav)
        self.assertEqual(chunks, [wav])

    def test_split_wav_for_esp_oversized(self) -> None:
        wav = _make_wav(30.0)
        self.assertGreater(len(wav), playback.ESP_MAX_PLAY_WAV_BYTES)
        chunks = playback.split_wav_for_esp(wav)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), playback.ESP_MAX_PLAY_WAV_BYTES)

    def test_extend_playback_busy_covers_clip(self) -> None:
        wav = _make_wav(3.0)
        busy_s = playback.extend_playback_busy(wav, device_id="dev-a")
        self.assertGreaterEqual(busy_s, 3.0)
        self.assertTrue(playback.device_busy_speaking("dev-a"))

    def test_wait_device_playback_idle(self) -> None:
        playback.mark_device_busy_for(0.2, device_id="dev-a")
        self.assertTrue(playback.wait_device_playback_idle("dev-a", timeout_s=2.0))


if __name__ == "__main__":
    unittest.main()
