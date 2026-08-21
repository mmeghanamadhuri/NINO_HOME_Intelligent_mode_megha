"""Voice commands: track my face / stop tracking / what do you see."""

from __future__ import annotations

import io
import os
import unittest
import wave
from unittest.mock import MagicMock, patch

import numpy as np

from voice_service import (
    VoiceReplyMeta,
    apply_face_track_command,
    esp_face_track_url,
    is_what_do_you_see_command,
    parse_face_track_command,
    process_voice_wav,
)


def _tone_wav(seconds: float = 1.0, rate: int = 16000) -> bytes:
    samples = (
        8000 * np.sin(2 * np.pi * 220 * np.arange(int(rate * seconds)) / rate)
    ).astype(np.int16)
    bio = io.BytesIO()
    with wave.open(bio, "wb") as wo:
        wo.setnchannels(1)
        wo.setsampwidth(2)
        wo.setframerate(rate)
        wo.writeframes(samples.tobytes())
    return bio.getvalue()


def _process_continue(heard: str, **kwargs):
    dummy = _tone_wav(0.1)
    os.environ["VOICE_MIN_ENERGY"] = "5"
    with (
        patch("voice_service.transcribe_wav", return_value=(heard, "mock")),
        patch("voice_service.synthesize_sapi_wav_bytes", return_value=(dummy, "mock")),
        patch("voice_service.resample_wav_bytes_to_mono_16bit", return_value=dummy),
        patch(
            "voice_service.last_tts_synthesis_info",
            return_value={"provider": "mock", "voice": "mock"},
        ),
    ):
        return process_voice_wav(
            _tone_wav(),
            session_kind="continue",
            aux_energy=99,
            device_id="30eda0e34fc4",
            voice_turn=3,
            session_id="s1",
            **kwargs,
        )


class FaceTrackPhraseTests(unittest.TestCase):
    def test_track_on_phrases(self) -> None:
        for text in (
            "track my face",
            "Track my face please",
            "see me while I'm talking",
            "see me while I am talking",
            "look at me while I'm talking",
            "watch me while I speak",
            "watch me while I'm speaking",
            "watch me while I am talking",
            "keep looking at me",
            "keep looking at my face",
            "follow my face",
            "start tracking my face",
            "enable face tracking",
        ):
            with self.subTest(text=text):
                self.assertIs(parse_face_track_command(text), True)

    def test_track_off_phrases(self) -> None:
        for text in (
            "stop tracking",
            "stop tracking my face",
            "don't track my face",
            "do not track my face",
            "dont track my face",
            "stop following my face",
            "turn off tracking",
            "disable face tracking",
        ):
            with self.subTest(text=text):
                self.assertIs(parse_face_track_command(text), False)

    def test_stop_alone_is_not_track_off(self) -> None:
        self.assertIsNone(parse_face_track_command("stop"))
        self.assertIsNone(parse_face_track_command("please stop"))

    def test_unrelated_speech_does_not_match(self) -> None:
        self.assertIsNone(parse_face_track_command("what's the weather"))
        self.assertIsNone(parse_face_track_command("what do you see"))
        self.assertIsNone(parse_face_track_command(""))

    def test_stop_tracking_wins_over_track_my_face_substring(self) -> None:
        self.assertIs(parse_face_track_command("stop tracking my face"), False)


class FaceTrackApplyTests(unittest.TestCase):
    @patch("voice_service.set_esp_face_track", return_value=(True, None))
    def test_on_posts_enabled_true(self, set_track: MagicMock) -> None:
        handled, reply = apply_face_track_command(
            "track my face", device_id="30eda0e34fc4"
        )
        self.assertTrue(handled)
        self.assertIn("looking at you", reply.lower())
        set_track.assert_called_once_with(True, device_id="30eda0e34fc4")

    @patch("voice_service.set_esp_face_track", return_value=(True, None))
    def test_off_confirmation(self, set_track: MagicMock) -> None:
        handled, reply = apply_face_track_command("stop tracking my face")
        self.assertTrue(handled)
        self.assertIn("stop tracking", reply.lower())
        set_track.assert_called_once_with(False, device_id=None)

    @patch("voice_service.set_esp_face_track", return_value=(False, "no_esp_url"))
    def test_no_device_url(self, _set_track: MagicMock) -> None:
        handled, reply = apply_face_track_command("track my face")
        self.assertTrue(handled)
        self.assertIn("looking at you", reply.lower())


class WhatDoYouSeePhraseTests(unittest.TestCase):
    def test_what_do_you_see_phrases(self) -> None:
        for text in (
            "what do you see",
            "What do you see?",
            "what can you see",
            "tell me what you see",
            "what's in front of you",
            "what is in front of you",
            "what do you see now",
            "describe what you see",
            "what are you seeing",
        ):
            with self.subTest(text=text):
                self.assertTrue(is_what_do_you_see_command(text), text)

    def test_not_what_do_you_see(self) -> None:
        self.assertFalse(is_what_do_you_see_command("who am I"))
        self.assertFalse(is_what_do_you_see_command("can you see me"))
        self.assertFalse(is_what_do_you_see_command("track my face"))
        self.assertFalse(is_what_do_you_see_command("see me while I'm talking"))
        self.assertFalse(is_what_do_you_see_command(""))

    def test_look_scan_meta_defaults_off(self) -> None:
        self.assertFalse(VoiceReplyMeta().look_scan)


class FaceTrackUrlTests(unittest.TestCase):
    def test_posts_to_face_track_path(self) -> None:
        with patch(
            "esp_playback.device_base_url", return_value="http://192.168.1.10/"
        ):
            self.assertEqual(
                esp_face_track_url("30eda0e34fc4"),
                "http://192.168.1.10/face/track",
            )


class VoicePipelineInterceptTests(unittest.TestCase):
    def test_track_my_face_posts_and_skips_llm(self) -> None:
        with (
            patch(
                "voice_service.set_esp_face_track", return_value=(True, None)
            ) as set_track,
            patch("llm_service.answer_voice_query") as llm,
        ):
            _out, meta = _process_continue("track my face")
        self.assertEqual(meta.timings["reply_path"], "face_track")
        self.assertFalse(meta.look_scan)
        self.assertTrue(meta.face_track)
        self.assertIn("looking at you", meta.timings["reply_text"].lower())
        set_track.assert_called_once_with(True, device_id="30eda0e34fc4")
        llm.assert_not_called()

    def test_stop_tracking_posts_enabled_false(self) -> None:
        with patch(
            "voice_service.set_esp_face_track", return_value=(True, None)
        ) as set_track:
            _out, meta = _process_continue("stop tracking my face")
        self.assertEqual(meta.timings["reply_path"], "face_track")
        self.assertIs(meta.face_track, False)
        set_track.assert_called_once_with(False, device_id="30eda0e34fc4")

    def test_watch_me_while_i_speak_enables_tracking(self) -> None:
        with (
            patch(
                "voice_service.set_esp_face_track", return_value=(True, None)
            ) as set_track,
            patch("llm_service.answer_voice_query") as llm,
        ):
            _out, meta = _process_continue("watch me while I speak")
        self.assertEqual(meta.timings["reply_path"], "face_track")
        self.assertTrue(meta.face_track)
        set_track.assert_called_once_with(True, device_id="30eda0e34fc4")
        llm.assert_not_called()

    def test_what_do_you_see_skips_llm_and_sets_look_scan(self) -> None:
        with (
            patch(
                "voice_service.snapshot_visible_scene",
                return_value=(["Hari"], [{"label": "laptop"}]),
            ),
            patch("llm_service.answer_voice_query") as llm,
        ):
            _out, meta = _process_continue("what do you see")
        self.assertEqual(meta.timings["reply_path"], "look_scan")
        self.assertTrue(meta.look_scan)
        self.assertIsNone(meta.motion)
        self.assertIn("hari", meta.timings["reply_text"].lower())
        self.assertIn("laptop", meta.timings["reply_text"].lower())
        llm.assert_not_called()

    def test_next_utterance_applies_scene_without_register(self) -> None:
        ident = MagicMock()
        ident.should_skip_prompt_echo.return_value = False
        ident.in_registration.return_value = False
        ident.is_active.return_value = True
        ident.current_user.return_value = ("Hari", False)
        ident.apply_visible_scene.return_value = None
        with patch("session_identity.get_session_identity", return_value=ident):
            _process_continue(
                "what time is it",
                visible_names=["Nora"],
                camera_identity_name="Nora",
                camera_identity_state="recognized",
            )
        ident.apply_visible_scene.assert_called_once()
        kwargs = ident.apply_visible_scene.call_args.kwargs
        self.assertEqual(kwargs["visible_names"], ["Nora"])
        self.assertEqual(kwargs["scene_state"], "recognized")
        self.assertIs(kwargs["allow_register"], False)

    def test_look_scan_turn_still_refreshes_identity(self) -> None:
        ident = MagicMock()
        ident.should_skip_prompt_echo.return_value = False
        ident.in_registration.return_value = False
        ident.is_active.return_value = True
        ident.current_user.return_value = ("Hari", False)
        ident.apply_visible_scene.return_value = None
        with (
            patch("session_identity.get_session_identity", return_value=ident),
            patch(
                "voice_service.snapshot_visible_scene",
                return_value=(["Hari"], []),
            ),
        ):
            _out, meta = _process_continue(
                "what do you see",
                visible_names=["Hari"],
                camera_identity_state="recognized",
                camera_identity_name="Hari",
            )
        self.assertTrue(meta.look_scan)
        ident.apply_visible_scene.assert_called_once()
        self.assertIs(
            ident.apply_visible_scene.call_args.kwargs["allow_register"], False
        )


if __name__ == "__main__":
    unittest.main()
