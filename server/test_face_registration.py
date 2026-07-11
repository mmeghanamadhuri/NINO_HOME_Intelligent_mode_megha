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
        self.faces.validate_registration_name.return_value = (True, None)
        self.faces.identify_registered_face.return_value = None
        self.faces.same_person.return_value = False
        self.faces.match_soft_threshold = 0.42
        self.read_frame = MagicMock(return_value=None)
        self.svc = FaceRegistrationService(self.faces, self.read_frame)
        self.svc.unknown_seconds = 1.0
        self.svc.prompt_cooldown_seconds = 0.0
        self.svc.unknown_confirm_frames = 1

    def _unknown_results(self) -> list[dict]:
        return [
            {
                "primary": True,
                "recognized": False,
                "stabilized": False,
                "name": "Unknown",
                "detection_valid": True,
                "registration_eligible": True,
                "detection_score": 0.85,
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
            self.svc._unknown_streak = self.svc.unknown_confirm_frames
            self.svc.on_frame(results)
        self.assertEqual(self.svc.state, "awaiting_name")
        post_wav.assert_called_once()
        self.assertTrue(post_wav.call_args.kwargs.get("prompt_ack"))
        self.assertTrue(post_wav.call_args.kwargs.get("prompt_ack_chime"))

    def test_on_frame_ignores_unvalidated_unknown(self) -> None:
        results = [
            {
                "primary": True,
                "recognized": False,
                "stabilized": False,
                "name": "Unknown",
                "detection_valid": False,
                "registration_eligible": False,
                "box": {"x": 10, "y": 10, "w": 80, "h": 80},
            }
        ]
        with patch("face_registration_service.time.time", return_value=100.0):
            self.svc._unknown_since = 98.0
            self.svc._unknown_streak = 99
            self.svc.on_frame(results)
        self.assertEqual(self.svc.state, "idle")
        self.assertIsNone(self.svc._unknown_since)

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

    @patch("face_registration_service.post_wav_to_esp")
    @patch("face_registration_service.esp_play_wav_url", return_value="http://esp/play_wav")
    @patch("face_registration_service.FaceRegistrationService._synthesize_prompt_wav")
    def test_no_speech_retry_prompt(self, _synth, _url, post_wav) -> None:
        self.svc.no_speech_retry_seconds = 5.0
        self.svc.listen_open_delay_seconds = 0.0
        self.svc.max_no_speech_retries = 2
        with self.svc._lock:
            self.svc._state = "awaiting_name"
            self.svc._listen_prompt_at = 100.0
            self.svc._last_prompt_playback_seconds = 0.0
            self.svc._voice_heard_since_listen = False
            self.svc._no_speech_retries = 0

        with patch("face_registration_service.time.time", return_value=103.0):
            self.svc.on_frame([], voice_active=False)
        post_wav.assert_not_called()

        with patch("face_registration_service.time.time", return_value=106.0):
            self.svc.on_frame([], voice_active=False)

        self.assertEqual(self.svc.state, "awaiting_name")
        self.assertEqual(self.svc._no_speech_retries, 1)
        post_wav.assert_called_once()
        self.assertTrue(post_wav.call_args.kwargs.get("prompt_ack"))
        self.assertTrue(post_wav.call_args.kwargs.get("prompt_ack_chime"))

    @patch("face_registration_service.post_wav_to_esp")
    @patch("face_registration_service.esp_play_wav_url", return_value="http://esp/play_wav")
    @patch("face_registration_service.FaceRegistrationService._synthesize_prompt_wav")
    def test_no_speech_gives_up_after_max_retries(self, _synth, _url, post_wav) -> None:
        self.svc.no_speech_retry_seconds = 5.0
        self.svc.listen_open_delay_seconds = 0.0
        self.svc.max_no_speech_retries = 1
        with self.svc._lock:
            self.svc._state = "awaiting_name"
            self.svc._listen_prompt_at = 100.0
            self.svc._last_prompt_playback_seconds = 0.0
            self.svc._voice_heard_since_listen = False
            self.svc._no_speech_retries = 1

        with patch("face_registration_service.time.time", return_value=106.0):
            self.svc.on_frame([], voice_active=False)

        self.assertEqual(self.svc.state, "idle")
        post_wav.assert_not_called()

    def test_note_voice_received_blocks_no_speech_retry(self) -> None:
        self.svc.no_speech_retry_seconds = 5.0
        self.svc.listen_open_delay_seconds = 0.0
        with self.svc._lock:
            self.svc._state = "awaiting_name"
            self.svc._listen_prompt_at = 100.0
            self.svc._last_prompt_playback_seconds = 0.0
        self.svc.on_voice_query_started()

        with patch("face_registration_service.time.time", return_value=106.0):
            with patch("face_registration_service.post_wav_to_esp") as post_wav:
                self.svc.on_frame([], voice_active=False)
                post_wav.assert_not_called()

    def test_handle_voice_name_miss_schedules_relisten(self) -> None:
        with self.svc._lock:
            self.svc._state = "awaiting_name"
        result = self.svc.handle_voice("hello there")
        self.assertTrue(result.handled)
        self.assertTrue(result.relisten_after_reply)
        self.assertEqual(self.svc.state, "awaiting_name")

    def test_handle_voice_blocks_duplicate_name_for_known_face(self) -> None:
        frame = MagicMock()
        self.read_frame.return_value = frame
        self.faces.validate_registration_name.return_value = (False, "Chakri")
        with self.svc._lock:
            self.svc._state = "awaiting_name"

        result = self.svc.handle_voice("My name is Jaanu")

        self.assertTrue(result.handled)
        self.assertEqual(result.already_registered_as, "Chakri")
        self.assertIn("already registered as Chakri", result.reply)
        self.assertEqual(self.svc.state, "idle")
        self.faces.register_sample.assert_not_called()

    def test_handle_voice_allows_same_name_refresh(self) -> None:
        frame = MagicMock()
        self.read_frame.return_value = frame
        self.faces.validate_registration_name.return_value = (True, None)
        self.faces.identify_registered_face.return_value = "Chakri"
        self.faces.same_person.return_value = True
        with self.svc._lock:
            self.svc._state = "awaiting_name"

        with patch(
            "face_registration_service.capture_face_samples",
            return_value=MagicMock(saved_samples=5, training={"people": 1}, errors=[]),
        ):
            result = self.svc.handle_voice("My name is Chakri")

        self.assertTrue(result.handled)
        self.assertEqual(result.registered_name, "Chakri")
        self.assertIn("added more face samples", result.reply)

    def test_on_frame_cancels_awaiting_when_recognized(self) -> None:
        with self.svc._lock:
            self.svc._state = "awaiting_name"
        results = [
            {
                "primary": True,
                "recognized": True,
                "stabilized": True,
                "name": "Chakri",
                "box": {"x": 10, "y": 10, "w": 80, "h": 80},
            }
        ]
        self.svc.on_frame(results)
        self.assertEqual(self.svc.state, "idle")


if __name__ == "__main__":
    unittest.main()
