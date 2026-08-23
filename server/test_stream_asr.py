"""Tests for streamed Aux-in end-of-speech."""

from __future__ import annotations

import array
import unittest

from stream_asr import (
    DEFAULT_CONTINUE_ENERGY,
    DEFAULT_MAX_MS,
    DEFAULT_REGISTER_MAX_MS,
    DEFAULT_START_ENERGY,
    StreamEndOfSpeech,
    UtteranceBuffer,
    pcm_frame_energy,
    stream_idle_timeout_ends_session,
    stream_listen_max_ms,
    stream_vad_from_environ,
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

    def test_mid_energy_after_speech_still_ends(self) -> None:
        """Aux idle often sits in 20–49; that band must not reset hangover."""
        vad = StreamEndOfSpeech(
            start_energy=50,
            continue_energy=55,
            quiet_energy=20,
            speech_ms=160,
            silence_ms=200,
            min_speech_ms=160,
            max_ms=5000,
        )
        for _ in range(10):
            vad.feed(_frame(200))
        ended = False
        for _ in range(20):
            if vad.feed(_frame(30)) == "end_of_speech":
                ended = True
                break
        self.assertTrue(ended)

    def test_ambient_12_25_closes_after_speech(self) -> None:
        """Sensitive start (12) must not keep a turn open on 12–25 ambient noise."""
        vad = StreamEndOfSpeech(
            start_energy=12,
            continue_energy=28,
            quiet_energy=5,
            speech_ms=60,
            silence_ms=500,
            min_speech_ms=60,
            max_ms=30000,
        )
        for _ in range(8):
            vad.feed(_frame(12))
        self.assertTrue(vad.heard_speech)
        for _ in range(30):
            vad.feed(_frame(18))
        ended = False
        for _ in range(40):
            if vad.feed(_frame(18)) == "end_of_speech":
                ended = True
                break
        self.assertTrue(ended)
        self.assertLess(vad.uttered_ms, 30000)

    def test_timeout_without_speech(self) -> None:
        vad = StreamEndOfSpeech(start_energy=50, max_ms=80, frame_ms=20)
        self.assertEqual(vad.feed(_frame(1)), "idle")
        self.assertEqual(vad.feed(_frame(1)), "idle")
        self.assertEqual(vad.feed(_frame(1)), "idle")
        self.assertEqual(vad.feed(_frame(1)), "timeout")

    def test_default_max_ms_is_30s(self) -> None:
        self.assertEqual(DEFAULT_MAX_MS, 30000)

    def test_register_max_ms_is_30s(self) -> None:
        self.assertEqual(DEFAULT_REGISTER_MAX_MS, 30000)
        self.assertEqual(stream_listen_max_ms(in_registration=True), 30000)
        self.assertEqual(stream_listen_max_ms(in_registration=False), 30000)
        self.assertEqual(
            stream_listen_max_ms(in_registration=True, register_max_ms=5000), 5000
        )

    def test_idle_timeout_ends_session_not_skip(self) -> None:
        self.assertTrue(stream_idle_timeout_ends_session("timeout"))
        self.assertFalse(stream_idle_timeout_ends_session("end_of_speech"))
        self.assertFalse(stream_idle_timeout_ends_session("skip"))
        self.assertFalse(
            stream_idle_timeout_ends_session("timeout", in_registration=True)
        )

    def test_30s_silence_times_out(self) -> None:
        vad = StreamEndOfSpeech(start_energy=50, max_ms=30000, frame_ms=20)
        for _ in range((30000 // 20) - 1):
            self.assertEqual(vad.feed(_frame(1)), "idle")
        self.assertEqual(vad.feed(_frame(1)), "timeout")
        self.assertFalse(vad.heard_speech)

    def test_30s_register_silence_times_out(self) -> None:
        vad = StreamEndOfSpeech(start_energy=50, max_ms=30000, frame_ms=20)
        for _ in range((30000 // 20) - 1):
            self.assertEqual(vad.feed(_frame(1)), "idle")
        self.assertEqual(vad.feed(_frame(1)), "timeout")
        self.assertFalse(vad.heard_speech)

    def test_buffer_listen_max_ms(self) -> None:
        buf = UtteranceBuffer()
        buf.set_listen_max_ms(60000)
        self.assertEqual(buf.vad.max_ms, 60000)

    def test_buffer_accumulates_pcm(self) -> None:
        buf = UtteranceBuffer()
        buf.vad = StreamEndOfSpeech(start_energy=50, speech_ms=40, max_ms=5000)
        frame = _frame(300)
        buf.feed(frame)
        self.assertEqual(len(buf.pcm), len(frame))

    def test_pcm_energy(self) -> None:
        self.assertGreater(pcm_frame_energy(_frame(120)), 50)
        self.assertLess(pcm_frame_energy(_frame(3)), 10)

    def test_quiet_noise_floor_detects_soft_speech(self) -> None:
        vad = StreamEndOfSpeech(
            start_energy=50,
            quiet_energy=20,
            speech_ms=160,
            max_ms=5000,
        )
        for _ in range(20):
            self.assertEqual(vad.feed(_frame(3)), "idle")
        self.assertLessEqual(vad.effective_start(), 18)
        states = [vad.feed(_frame(40)) for _ in range(10)]
        self.assertIn("speech", states)
        self.assertTrue(vad.heard_speech)
        self.assertGreaterEqual(vad.peak_energy, 40)

    def test_feeble_speech_on_quiet_mic(self) -> None:
        vad = StreamEndOfSpeech(
            start_energy=12,
            continue_energy=28,
            quiet_energy=5,
            speech_ms=60,
        )
        for _ in range(20):
            self.assertEqual(vad.feed(_frame(2)), "idle")
        self.assertLessEqual(vad.effective_start(), 12)
        states = [vad.feed(_frame(12)) for _ in range(8)]
        self.assertIn("speech", states)
        self.assertTrue(vad.heard_speech)

    def test_electrical_ticks_stay_idle(self) -> None:
        vad = StreamEndOfSpeech(start_energy=12, quiet_energy=5, speech_ms=60)
        for _ in range(30):
            self.assertEqual(vad.feed(_frame(2)), "idle")
        self.assertFalse(vad.heard_speech)

    def test_high_noise_keeps_absolute_start(self) -> None:
        vad = StreamEndOfSpeech(
            start_energy=50,
            quiet_energy=20,
            speech_ms=160,
            max_ms=5000,
        )
        for _ in range(20):
            vad.feed(_frame(18))
        self.assertEqual(vad.effective_start(), 50)
        for _ in range(10):
            self.assertEqual(vad.feed(_frame(25)), "idle")
        self.assertFalse(vad.heard_speech)


class SharedListenEnergyTests(unittest.TestCase):
    def test_wake_and_session_use_the_same_start(self) -> None:
        vad = stream_vad_from_environ()
        self.assertEqual(vad.start_energy, DEFAULT_START_ENERGY)
        self.assertEqual(vad.continue_energy, DEFAULT_CONTINUE_ENERGY)
        self.assertEqual(DEFAULT_START_ENERGY, 12)
        buf = UtteranceBuffer()
        self.assertEqual(buf.vad.start_energy, DEFAULT_START_ENERGY)

    def test_soft_speech_starts_a_turn(self) -> None:
        vad = stream_vad_from_environ()
        states = [vad.feed(_frame(16)) for _ in range(8)]
        self.assertIn("speech", states)
        self.assertTrue(vad.heard_speech)


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
