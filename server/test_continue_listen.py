"""Tests for post-LLM-reply continue-listen (mic wake without wake word)."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from voice_service import (
    conversation_goodbye_reply,
    is_conversation_goodbye,
    should_continue_listen_after_reply,
)


class ConversationGoodbyeTests(unittest.TestCase):
    def test_goodbye_phrases(self) -> None:
        for text in (
            "stop",
            "Stop",
            "please stop",
            "goodbye",
            "Good bye",
            "bye",
            "Bye",
            "Bye.",
            "ok bye",
            "bye bye",
            "see you later",
            "talk to you later",
            "that's all",
            "I'm done",
            "stop listening",
            "end the conversation",
        ):
            with self.subTest(text=text):
                self.assertTrue(is_conversation_goodbye(text))

    def test_not_goodbye(self) -> None:
        for text in (
            "what is a microcontroller",
            "tell me a joke",
            "who am I",
            "continue",
            "nearby shops",
        ):
            with self.subTest(text=text):
                self.assertFalse(is_conversation_goodbye(text))

    def test_stop_the_music_is_not_session_end(self) -> None:
        self.assertFalse(is_conversation_goodbye("stop the music"))

    def test_goodbye_reply_has_no_follow_up(self) -> None:
        reply = conversation_goodbye_reply()
        self.assertTrue(reply.strip())
        lower = reply.lower()
        self.assertFalse("?" in reply)
        self.assertNotIn("what's", lower)
        self.assertNotIn("how can i", lower)


class ContinueListenGateTests(unittest.TestCase):
    def test_llm_paths_continue(self) -> None:
        for path in (
            "llm",
            "identity_llm",
            "memory_llm_store",
            "memory_llm_recall",
            "recap",
            "recap_answer",
            "recap_not_found",
            "last_question",
            "joke",
        ):
            with self.subTest(path=path):
                self.assertTrue(
                    should_continue_listen_after_reply(path, "tell me more")
                )

    def test_non_llm_paths_skip(self) -> None:
        for path in (
            "goodbye",
            "wake_reject",
            "music_play",
            "music_stop",
            "music_now_playing",
            "music_not_found",
            "music_unavailable",
            "music_no_device",
        ):
            with self.subTest(path=path):
                self.assertFalse(
                    should_continue_listen_after_reply(path, "set volume to 50")
                )

    def test_goodbye_stops_listen(self) -> None:
        self.assertFalse(should_continue_listen_after_reply("llm", "goodbye"))
        self.assertFalse(should_continue_listen_after_reply("llm", "Bye"))
        self.assertFalse(should_continue_listen_after_reply("goodbye", "bye"))

    def test_idle_timeout_goodbye_ends_session(self) -> None:
        from voice_service import VoiceReplyMeta, synthesize_idle_goodbye_wav

        dummy = b"RIFF" + b"\x00" * 40
        with patch(
            "voice_service.synthesize_sapi_wav_bytes",
            return_value=(dummy, "mock"),
        ), patch(
            "voice_service.resample_wav_bytes_to_mono_16bit",
            return_value=dummy,
        ), patch(
            "voice_service._wav_seconds",
            return_value=0.5,
        ), patch(
            "device_session.clear_device_session",
        ), patch(
            "math_voice.clear_math_quiz",
        ), patch(
            "conversation_sessions.end_session",
        ) as persist:
            wav, meta = synthesize_idle_goodbye_wav(
                session_id="sid-idle", device_id="nino-home"
            )
        self.assertEqual(wav, dummy)
        self.assertIsInstance(meta, VoiceReplyMeta)
        self.assertTrue(meta.end_session)
        self.assertEqual(meta.timings.get("reply_path"), "goodbye")
        persist.assert_called_once()
        self.assertEqual(persist.call_args.kwargs.get("reason"), "idle_timeout")

    def test_env_disable(self) -> None:
        with patch.dict(os.environ, {"VOICE_CONTINUE_LISTEN": "0"}):
            self.assertFalse(
                should_continue_listen_after_reply("llm", "tell me more")
            )


if __name__ == "__main__":
    unittest.main()
