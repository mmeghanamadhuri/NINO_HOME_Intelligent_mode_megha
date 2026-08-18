"""Tests for streamed Aux-in end-of-speech."""

from __future__ import annotations

import array
import unittest

from stream_asr import StreamEndOfSpeech, UtteranceBuffer, pcm_frame_energy


def _frame(energy: int, samples: int = 320) -> bytes:
    value = max(-32767, min(32767, energy))
    buf = array.array("h", [value] * samples)
    return buf.tobytes()


class StreamEndOfSpeechTests(unittest.TestCase):
    def test_silence_stays_idle(self) -> None:
        vad = StreamEndOfSpeech(start_energy=50, quiet_energy=20, max_ms=5000)
        for _ in range(10):
            self.assertEqual(vad.feed(_frame(2)), "idle")
        self.assertFalse(vad.heard_speech)

    def test_speech_then_silence_ends(self) -> None:
        vad = StreamEndOfSpeech(
            start_energy=50,
            quiet_energy=20,
            speech_ms=160,
            silence_ms=200,
            min_speech_ms=160,
            max_ms=5000,
        )
        states = []
        for _ in range(10):
            states.append(vad.feed(_frame(200)))
        self.assertIn("speech", states)
        ended = False
        for _ in range(20):
            if vad.feed(_frame(1)) == "end_of_speech":
                ended = True
                break
        self.assertTrue(ended)

    def test_timeout_without_speech(self) -> None:
        vad = StreamEndOfSpeech(start_energy=50, max_ms=80, frame_ms=20)
        self.assertEqual(vad.feed(_frame(1)), "idle")
        self.assertEqual(vad.feed(_frame(1)), "idle")
        self.assertEqual(vad.feed(_frame(1)), "idle")
        self.assertEqual(vad.feed(_frame(1)), "timeout")

    def test_buffer_accumulates_pcm(self) -> None:
        buf = UtteranceBuffer()
        buf.vad = StreamEndOfSpeech(start_energy=50, speech_ms=40, max_ms=5000)
        frame = _frame(300)
        buf.feed(frame)
        self.assertEqual(len(buf.pcm), len(frame))

    def test_pcm_energy(self) -> None:
        self.assertGreater(pcm_frame_energy(_frame(120)), 50)
        self.assertLess(pcm_frame_energy(_frame(3)), 10)


if __name__ == "__main__":
    unittest.main()
