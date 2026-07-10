"""Tests for automatic voice face registration."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from face_registration_voice import extract_registration_name
from face_registration_service import FaceRegistrationService


class ExtractRegistrationNameTests(unittest.TestCase):
    def test_my_name_is(self) -> None:
        self.assertEqual(extract_registration_name("My name is Sirena"), "Sirena")

    def test_im_phrase(self) -> None:
        self.assertEqual(extract_registration_name("I'm Chakri"), "Chakri")

    def test_call_me(self) -> None:
        self.assertEqual(extract_registration_name("Please call me Teja"), "Teja")

    def test_single_name(self) -> None:
        self.assertEqual(extract_registration_name("Sameera"), "Sameera")

    def test_rejects_greeting(self) -> None:
        self.assertIsNone(extract_registration_name("Hello there"))

    def test_rejects_register_command(self) -> None:
        self.assertIsNone(extract_registration_name("Register my face"))


class FaceRegistrationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.faces = MagicMock()
        self.faces.register_sample.return_value = MagicMock()
        self.faces.train.return_value = {"people": 1, "samples": 2}
        self.read_frame = MagicMock(return_value=None)
        self.svc = FaceRegistrationService(self.faces, self.read_frame)
        self.svc.unknown_seconds = 1.0
        self.svc.prompt_cooldown_seconds = 0.0

    def _unknown_results(self) -> list[dict]:
        return [
            {
                "primary": True,
                "recognized": False,
                "stabilized": False,
                "name": "Unknown",
                "box": {"x": 10, "y": 10, "w": 80, "h": 80},
            }
        ]

    @patch("face_registration_service.post_wav_to_esp")
    @patch("face_registration_service.esp_play_wav_url", return_value="http://esp/play_wav")
    @patch("face_registration_service.FaceRegistrationService._synthesize_prompt_wav")
    def test_on_frame_prompts_after_stable_unknown(
        self, _synth, _url, post_wav
    ) -> None:
        results = self._unknown_results()
        self.svc.on_frame(results)
        self.assertEqual(self.svc.state, "idle")
        with patch("face_registration_service.time.time", return_value=100.0):
            self.svc._unknown_since = 98.0
            self.svc.on_frame(results)
        self.assertEqual(self.svc.state, "awaiting_name")
        post_wav.assert_called_once()
        self.assertTrue(post_wav.call_args.kwargs.get("prompt_ack"))
        self.assertTrue(post_wav.call_args.kwargs.get("prompt_ack_chime"))

    def test_handle_voice_registers_name(self) -> None:
        frame = MagicMock()
        self.read_frame.return_value = frame
        with self.svc._lock:
            self.svc._state = "awaiting_name"

        with patch(
            "face_registration_service.capture_face_samples",
            return_value=MagicMock(saved_samples=5, training={"people": 1}, errors=[]),
        ):
            result = self.svc.handle_voice("My name is Alex")

        self.assertTrue(result.handled)
        self.assertEqual(result.registered_name, "Alex")
        self.assertEqual(self.svc.state, "idle")

    def test_handle_voice_ignored_when_idle(self) -> None:
        result = self.svc.handle_voice("My name is Alex")
        self.assertFalse(result.handled)


if __name__ == "__main__":
    unittest.main()
