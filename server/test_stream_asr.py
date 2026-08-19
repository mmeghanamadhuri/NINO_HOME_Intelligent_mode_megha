"""Tests for streamed Aux-in end-of-speech."""

from __future__ import annotations

import array
import unittest

from stream_asr import (
    DEFAULT_MAX_MS,
    StreamEndOfSpeech,
    UtteranceBuffer,
    pcm_frame_energy,
    stream_idle_timeout_ends_session,
)


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

    def test_default_max_ms_is_30s(self) -> None:
        self.assertEqual(DEFAULT_MAX_MS, 30000)

    def test_idle_timeout_ends_session_not_skip(self) -> None:
        self.assertTrue(stream_idle_timeout_ends_session("timeout"))
        self.assertFalse(stream_idle_timeout_ends_session("end_of_speech"))
        self.assertFalse(stream_idle_timeout_ends_session("skip"))

    def test_30s_silence_times_out(self) -> None:
        vad = StreamEndOfSpeech(start_energy=50, max_ms=30000, frame_ms=20)
        for _ in range((30000 // 20) - 1):
            self.assertEqual(vad.feed(_frame(1)), "idle")
        self.assertEqual(vad.feed(_frame(1)), "timeout")
        self.assertFalse(vad.heard_speech)

    def test_buffer_accumulates_pcm(self) -> None:
        buf = UtteranceBuffer()
        buf.vad = StreamEndOfSpeech(start_energy=50, speech_ms=40, max_ms=5000)
        frame = _frame(300)
        buf.feed(frame)
        self.assertEqual(len(buf.pcm), len(frame))

    def test_pcm_energy(self) -> None:
        self.assertGreater(pcm_frame_energy(_frame(120)), 50)
        self.assertLess(pcm_frame_energy(_frame(3)), 10)


class StreamPcmFrameDetectTests(unittest.TestCase):
    def test_640_byte_pcm_is_stream_frame(self) -> None:
        from stream_asr import looks_like_stream_pcm_frame

        self.assertTrue(looks_like_stream_pcm_frame(_frame(80)))

    def test_wav_header_is_not_stream_frame(self) -> None:
        from stream_asr import looks_like_stream_pcm_frame

        wav = b"RIFF" + b"\x00" * 4 + b"WAVE" + b"\x00" * 640
        self.assertFalse(looks_like_stream_pcm_frame(wav))

    def test_full_clip_is_not_stream_frame(self) -> None:
        from stream_asr import looks_like_stream_pcm_frame

        self.assertFalse(looks_like_stream_pcm_frame(_frame(80, samples=8000)))

    def test_empty_is_not_stream_frame(self) -> None:
        from stream_asr import looks_like_stream_pcm_frame

        self.assertFalse(looks_like_stream_pcm_frame(b""))
        self.assertFalse(looks_like_stream_pcm_frame(None))


if __name__ == "__main__":
    unittest.main()
