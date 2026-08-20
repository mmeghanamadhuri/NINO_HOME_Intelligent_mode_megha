"""Tests for automatic voice face registration."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch
import time

from face_registration_voice import (
    extract_registration_name,
    is_face_reg_prompt_echo,
    is_incomplete_name_phrase,
    is_registration_cancel,
)
from face_registration_service import (
    FaceRegistrationService,
    INCOMPLETE_NAME_PROMPT,
    NAME_RETRY_PROMPT,
    NO_SPEECH_RETRY_PROMPT,
    REGISTRATION_CANCEL_REPLY,
    REGISTRATION_PROMPTS,
    pick_registration_prompt,
)


class ExtractRegistrationNameTests(unittest.TestCase):
    def test_my_name_is(self) -> None:
        self.assertEqual(extract_registration_name("My name is Sirena"), "Sirena")

    def test_im_phrase(self) -> None:
        self.assertEqual(extract_registration_name("I'm Chakri"), "Chakri")

    def test_call_me(self) -> None:
        self.assertEqual(extract_registration_name("Please call me Teja"), "Teja")

    def test_single_name(self) -> None:
        self.assertEqual(extract_registration_name("Sameera"), "Sameera")

    def test_framed_short_real_name(self) -> None:
        self.assertEqual(extract_registration_name("My name is Sam"), "Sam")

    def test_rejects_greeting(self) -> None:
        self.assertIsNone(extract_registration_name("Hello there"))

    def test_rejects_register_command(self) -> None:
        self.assertIsNone(extract_registration_name("Register my face"))

    def test_incomplete_my_name_is(self) -> None:
        self.assertTrue(is_incomplete_name_phrase("My name is"))
        self.assertTrue(is_incomplete_name_phrase("My name is."))
        self.assertIsNone(extract_registration_name("My name is"))

    def test_accepts_devanagari_name(self) -> None:
        self.assertEqual(extract_registration_name("दयानंद"), "दयानंद")

    def test_rejects_latency_junk_names(self) -> None:
        for heard in (
            "Enough",
            "turn off",
            "C",
            "Cha",
            "Greek",
            "Agree",
            "See you soon again.",
            "Tell me a joke",
            "My name is Cha",
            "My name is Agree",
            "Not anything after the beep. Please tell your name.",
            "After the date, peace in your name.",
        ):
            self.assertIsNone(extract_registration_name(heard), heard)

    def test_rejects_too_short_bare_name(self) -> None:
        self.assertIsNone(extract_registration_name("Sam"))
        self.assertIsNone(extract_registration_name("Ali"))

    def test_detects_prompt_echo(self) -> None:
        echo = "I don't quite catch your name, please say it again."
        self.assertTrue(is_face_reg_prompt_echo(echo))
        self.assertIsNone(extract_registration_name(echo))

    def test_entertaining_prompts_are_echo_safe(self) -> None:
        self.assertGreaterEqual(len(REGISTRATION_PROMPTS), 6)
        for prompt in REGISTRATION_PROMPTS:
            self.assertIn("After the beep", prompt)
            self.assertIn("say your name", prompt.lower())
            self.assertTrue(is_face_reg_prompt_echo(prompt), prompt)
            self.assertIsNone(extract_registration_name(prompt), prompt)
        for prompt in (NAME_RETRY_PROMPT, INCOMPLETE_NAME_PROMPT, NO_SPEECH_RETRY_PROMPT):
            self.assertTrue(is_face_reg_prompt_echo(prompt), prompt)
            self.assertIsNone(extract_registration_name(prompt), prompt)

    def test_session_greet_echo(self) -> None:
        from face_registration_voice import is_opening_greeting_echo

        self.assertTrue(is_opening_greeting_echo("Hey Hari, how can I help you"))
        self.assertTrue(is_opening_greeting_echo("Hey Hari"))
        self.assertTrue(is_opening_greeting_echo("how can I help you"))
        self.assertTrue(is_opening_greeting_echo("Good morning Hari! How are you today?"))
        self.assertFalse(is_opening_greeting_echo("What's the weather in London?"))
        self.assertFalse(is_opening_greeting_echo("Hi tell me a joke"))

    def test_pick_registration_prompt_from_pool(self) -> None:
        picked = {pick_registration_prompt() for _ in range(40)}
        self.assertTrue(picked.issubset(set(REGISTRATION_PROMPTS)))
        self.assertGreater(len(picked), 1)

    def test_detects_registration_cancel(self) -> None:
        for heard in (
            "no",
            "Nope",
            "cancel",
            "shut up",
            "stop",
            "never mind",
            "no thanks",
            "I don't want to",
            "leave me alone",
            "forget it",
        ):
            self.assertTrue(is_registration_cancel(heard), heard)
            self.assertIsNone(extract_registration_name(heard), heard)

    def test_cancel_does_not_override_framed_name(self) -> None:
        self.assertFalse(is_registration_cancel("My name is Nora"))
        self.assertEqual(extract_registration_name("My name is Nora"), "Nora")


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
        self.svc.enabled = True
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

    @patch("face_registration_service.deliver_wav_to_device")
    @patch("face_registration_service.device_base_url", return_value="http://esp")
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

    @patch("face_registration_service.deliver_wav_to_device")
    @patch("face_registration_service.device_base_url", return_value="http://esp")
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

    @patch("face_registration_service.deliver_wav_to_device")
    @patch("face_registration_service.device_base_url", return_value="http://esp")
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
            with patch("face_registration_service.deliver_wav_to_device") as post_wav:
                self.svc.on_frame([], voice_active=False)
                post_wav.assert_not_called()

    def test_handle_voice_name_miss_schedules_relisten(self) -> None:
        with self.svc._lock:
            self.svc._state = "awaiting_name"
        result = self.svc.handle_voice("hello there")
        self.assertTrue(result.handled)
        self.assertTrue(result.relisten_after_reply)
        self.assertEqual(self.svc.state, "awaiting_name")

    def test_handle_voice_cancel_stops_registration(self) -> None:
        with self.svc._lock:
            self.svc._state = "awaiting_name"
            self.svc._last_prompt_at = 0.0
        result = self.svc.handle_voice("cancel")
        self.assertTrue(result.handled)
        self.assertFalse(result.relisten_after_reply)
        self.assertEqual(result.reply, REGISTRATION_CANCEL_REPLY)
        self.assertEqual(self.svc.state, "idle")
        self.assertIsNone(result.registered_name)
        self.assertGreater(self.svc._last_prompt_at, 0.0)
        self.faces.register_sample.assert_not_called()

    def test_handle_voice_no_stops_registration(self) -> None:
        with self.svc._lock:
            self.svc._state = "awaiting_name"
        result = self.svc.handle_voice("no")
        self.assertTrue(result.handled)
        self.assertEqual(result.reply, REGISTRATION_CANCEL_REPLY)
        self.assertEqual(self.svc.state, "idle")

    def test_handle_voice_incomplete_name_phrase(self) -> None:
        with self.svc._lock:
            self.svc._state = "awaiting_name"
        result = self.svc.handle_voice("My name is")
        self.assertTrue(result.handled)
        self.assertTrue(result.relisten_after_reply)
        self.assertEqual(result.reply, INCOMPLETE_NAME_PROMPT)
        self.assertEqual(self.svc._pending_relisten_prompt, INCOMPLETE_NAME_PROMPT)

    @patch("face_registration_service.deliver_wav_to_device")
    @patch("face_registration_service.device_base_url", return_value="http://esp")
    def test_handle_voice_prompt_echo_silent_relisten(self, _url, post_wav) -> None:
        with self.svc._lock:
            self.svc._state = "awaiting_name"
        result = self.svc.handle_voice(
            "I don't quite catch your name, please say it again."
        )
        self.assertTrue(result.handled)
        self.assertTrue(result.relisten_after_reply)
        self.assertEqual(result.reply, "")
        self.svc.relisten_after_missed_name()
        post_wav.assert_called_once()
        self.assertTrue(post_wav.call_args.kwargs.get("prompt_ack_chime"))

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

    def test_on_frame_keeps_awaiting_on_pending_match(self) -> None:
        with self.svc._lock:
            self.svc._state = "awaiting_name"
        results = [
            {
                "primary": True,
                "recognized": False,
                "stabilized": False,
                "pending": True,
                "candidate_name": "Chakri",
                "candidate_score": 0.43,
                "name": "Unknown",
                "box": {"x": 10, "y": 10, "w": 80, "h": 80},
            }
        ]
        self.svc.on_frame(results)
        self.assertEqual(self.svc.state, "awaiting_name")

    def test_on_frame_keeps_awaiting_while_voice_heard(self) -> None:
        with self.svc._lock:
            self.svc._state = "awaiting_name"
            self.svc._voice_heard_since_listen = True
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
        self.assertEqual(self.svc.state, "awaiting_name")

    def test_handle_voice_resumes_after_listen_window_reset(self) -> None:
        frame = MagicMock()
        self.read_frame.return_value = frame
        with self.svc._lock:
            self.svc._state = "idle"
            self.svc._listen_prompt_at = time.time() - 5.0

        with patch(
            "face_registration_service.capture_face_samples",
            return_value=MagicMock(saved_samples=5, training={"people": 1}, errors=[]),
        ):
            result = self.svc.handle_voice("Hi, my name is Samira")

        self.assertTrue(result.handled)
        self.assertEqual(result.registered_name, "Samira")
        self.assertEqual(self.svc.state, "idle")


class LegacyAutoRegisterDisabledTests(unittest.TestCase):
    def test_default_does_not_prompt(self) -> None:
        faces = MagicMock()
        svc = FaceRegistrationService(faces, MagicMock(return_value=None))
        self.assertFalse(svc.enabled)
        results = [
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
        with patch(
            "face_registration_service.FaceRegistrationService._send_listen_prompt"
        ) as send:
            svc.on_frame(results)
            svc.on_frame(results)
            send.assert_not_called()
        self.assertEqual(svc.state, "idle")
        self.assertFalse(svc.accepts_registration_voice("My name is Alex"))


if __name__ == "__main__":
    unittest.main()
