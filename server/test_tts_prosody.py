"""Tests for mood-based Piper delivery."""

from __future__ import annotations

import io
import os
import unittest
import wave

import numpy as np

from tts_prosody import (
    infer_speech_style,
    piper_pitch_offset,
    piper_robotic_amount,
    pitch_shift_wav_bytes,
    prosody_for_style,
    robotic_color_wav_bytes,
)


def _sine_wav(seconds: float = 0.2, rate: int = 22050, freq: float = 220.0) -> bytes:
    n = int(seconds * rate)
    t = np.arange(n, dtype=np.float32) / rate
    pcm = (np.sin(2.0 * np.pi * freq * t) * 16000.0).astype(np.int16)
    output = io.BytesIO()
    with wave.open(output, "wb") as wo:
        wo.setnchannels(1)
        wo.setsampwidth(2)
        wo.setframerate(rate)
        wo.writeframes(pcm.tobytes())
    return output.getvalue()


class SpeechStyleTests(unittest.TestCase):
    def test_greeting(self) -> None:
        self.assertEqual(
            infer_speech_style("Hello Chakri, good to see you!"),
            "greeting",
        )
        self.assertEqual(
            infer_speech_style("Good morning Yugandhar. How are you?"),
            "greeting",
        )

    def test_sad_overrides_hello(self) -> None:
        self.assertEqual(
            infer_speech_style("Hello. I'm sorry to hear that happened."),
            "sad",
        )

    def test_sad_soft_line(self) -> None:
        self.assertEqual(
            infer_speech_style("I'm sorry you're having a rough day."),
            "sad",
        )

    def test_tired_line(self) -> None:
        self.assertEqual(
            infer_speech_style("You sound tired. Get some rest."),
            "tired",
        )

    def test_happy_line(self) -> None:
        self.assertEqual(
            infer_speech_style("That's wonderful news, I'm so glad!"),
            "happy",
        )

    def test_question_is_curious(self) -> None:
        self.assertEqual(
            infer_speech_style("Iron is a metal. Would you like to know more?"),
            "curious",
        )

    def test_sad_is_softer_and_lower(self) -> None:
        greeting = prosody_for_style("greeting")
        sad = prosody_for_style("sad")
        tired = prosody_for_style("tired")
        self.assertLessEqual(greeting.length_mul, 0.82)
        self.assertGreaterEqual(greeting.pitch_semitones, 2.8)
        self.assertGreaterEqual(greeting.volume_mul, 1.15)
        self.assertGreaterEqual(sad.length_mul, 1.28)
        self.assertLessEqual(sad.pitch_semitones, -3.2)
        self.assertLessEqual(sad.volume_mul, 0.62)
        self.assertGreaterEqual(tired.length_mul, 1.35)
        self.assertLessEqual(tired.pitch_semitones, -2.8)
        self.assertLess(sad.volume_mul, greeting.volume_mul)

    def test_pitch_shift_keeps_duration(self) -> None:
        wav = _sine_wav()
        with wave.open(io.BytesIO(wav), "rb") as wf:
            src_frames = wf.getnframes()
            src_rate = wf.getframerate()
        shifted = pitch_shift_wav_bytes(wav, 1.2)
        with wave.open(io.BytesIO(shifted), "rb") as wf:
            self.assertEqual(wf.getnframes(), src_frames)
            self.assertEqual(wf.getframerate(), src_rate)
            self.assertGreater(wf.getnframes(), 0)

    def test_pitch_offset_defaults_to_a_robotic_lift(self) -> None:
        previous = os.environ.get("PIPER_PITCH_SEMITONES")
        try:
            os.environ.pop("PIPER_PITCH_SEMITONES", None)
            self.assertAlmostEqual(piper_pitch_offset(), 4.0)
            os.environ["PIPER_PITCH_SEMITONES"] = "-1.5"
            self.assertAlmostEqual(piper_pitch_offset(), -1.5)
        finally:
            if previous is None:
                os.environ.pop("PIPER_PITCH_SEMITONES", None)
            else:
                os.environ["PIPER_PITCH_SEMITONES"] = previous

    def test_robotic_color_keeps_duration(self) -> None:
        wav = _sine_wav()
        with wave.open(io.BytesIO(wav), "rb") as wf:
            src_frames = wf.getnframes()
        colored = robotic_color_wav_bytes(wav, 0.75)
        with wave.open(io.BytesIO(colored), "rb") as wf:
            self.assertEqual(wf.getnframes(), src_frames)
            pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
            self.assertGreater(int(np.max(np.abs(pcm))), 0)
        previous = os.environ.get("PIPER_ROBOTIC")
        try:
            os.environ.pop("PIPER_ROBOTIC", None)
            self.assertAlmostEqual(piper_robotic_amount(), 0.0)
        finally:
            if previous is None:
                os.environ.pop("PIPER_ROBOTIC", None)
            else:
                os.environ["PIPER_ROBOTIC"] = previous

    def test_prosody_can_be_disabled(self) -> None:
        from tts_prosody import infer_speech_prosody

        previous = os.environ.get("PIPER_PROSODY")
        os.environ["PIPER_PROSODY"] = "0"
        try:
            self.assertEqual(infer_speech_prosody("Hello there!").style, "neutral")
        finally:
            if previous is None:
                os.environ.pop("PIPER_PROSODY", None)
            else:
                os.environ["PIPER_PROSODY"] = previous


if __name__ == "__main__":
    unittest.main()
