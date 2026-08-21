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

    def test_ffmpeg_pitch_shift_keeps_duration(self) -> None:
        import shutil

        from tts_prosody import pitch_shift_wav_bytes_ffmpeg

        if not shutil.which("ffmpeg"):
            self.skipTest("ffmpeg not installed")
        wav = _sine_wav()
        with wave.open(io.BytesIO(wav), "rb") as wf:
            src_frames = wf.getnframes()
            src_rate = wf.getframerate()
        shifted = pitch_shift_wav_bytes_ffmpeg(wav, 4.0)
        with wave.open(io.BytesIO(shifted), "rb") as wf:
            self.assertEqual(wf.getframerate(), src_rate)
            self.assertAlmostEqual(wf.getnframes() / float(src_rate), src_frames / float(src_rate), delta=0.03)
            pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
            self.assertGreater(int(np.max(np.abs(pcm))), 0)

    def test_pitch_offset_defaults_to_natural(self) -> None:
        previous = os.environ.get("PIPER_PITCH_SEMITONES")
        try:
            os.environ.pop("PIPER_PITCH_SEMITONES", None)
            self.assertAlmostEqual(piper_pitch_offset(), 0.0)
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

    def test_kokoro_prosody_maps_greeting_faster_and_brighter(self) -> None:
        from tts_service import kokoro_synthesis_kwargs

        previous = os.environ.get("PIPER_PROSODY")
        os.environ["PIPER_PROSODY"] = "1"
        os.environ["PIPER_PITCH_SEMITONES"] = "6"
        os.environ["KOKORO_SPEED"] = "1.0"
        try:
            greeting = kokoro_synthesis_kwargs("Hello Chakri, good to see you!")
            sad = kokoro_synthesis_kwargs("I'm sorry you're having a rough day.")
            self.assertEqual(greeting["style"], "greeting")
            self.assertEqual(sad["style"], "sad")
            self.assertGreater(float(greeting["speed"]), float(sad["speed"]))
            self.assertGreater(float(greeting["pitch"]), float(sad["pitch"]))
        finally:
            if previous is None:
                os.environ.pop("PIPER_PROSODY", None)
            else:
                os.environ["PIPER_PROSODY"] = previous
            os.environ.pop("KOKORO_SPEED", None)


class UnifiedAmyTtsTests(unittest.TestCase):
    def test_greet_register_same_piper_kwargs_as_replies(self) -> None:
        from session_identity import OFFER_REGISTER_PROMPT, greet_recognized_user
        from tts_service import piper_synthesis_config_kwargs

        offer = piper_synthesis_config_kwargs(OFFER_REGISTER_PROMPT)
        greet = piper_synthesis_config_kwargs(greet_recognized_user("Hari"))
        normal = piper_synthesis_config_kwargs("The weather is mild today.")
        question = piper_synthesis_config_kwargs(
            "Looks like you are a new user, can I register you?"
        )
        self.assertEqual(offer, greet)
        self.assertEqual(offer, normal)
        self.assertEqual(offer, question)
        self.assertGreaterEqual(offer["length_scale"], 0.5)
        self.assertLessEqual(offer["length_scale"], 2.0)

    def test_session_open_wav_is_16k_not_22050(self) -> None:
        from unittest.mock import patch

        from session_identity import OFFER_REGISTER_PROMPT
        from voice_service import VOICE_ASSIST_PLAYBACK_HZ, synthesize_session_open_wav

        src = _sine_wav(seconds=0.4, rate=22050)
        with patch(
            "voice_service.synthesize_sapi_wav_bytes",
            return_value=(src, "en_US-amy-medium"),
        ):
            out, meta = synthesize_session_open_wav(
                OFFER_REGISTER_PROMPT,
                reply_path="session_register_offer",
            )
        with wave.open(io.BytesIO(out), "rb") as wf:
            self.assertEqual(wf.getframerate(), VOICE_ASSIST_PLAYBACK_HZ)
            duration = wf.getnframes() / float(wf.getframerate())
        self.assertAlmostEqual(duration, 0.4, delta=0.05)
        self.assertFalse(meta.end_session)


if __name__ == "__main__":
    unittest.main()
