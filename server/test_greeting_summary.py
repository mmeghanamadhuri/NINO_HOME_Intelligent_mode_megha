"""Tests for face greeting prompts with Phase C daily summary."""

from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from llm_service import (
    StartupGreetingParts,
    _sanitize_closing_question,
    build_greeting_prompt,
    build_startup_closing_question_prompt,
    build_startup_greeting_hello,
    build_startup_greeting_yesterday,
    build_startup_summary_greeting_prompt,
    finalize_startup_greeting,
    parse_summary_topics_for_greeting,
    startup_greeting_from_summary,
    startup_greeting_parts_from_summary,
)
from tts_service import TTSService, _clamp_spoken_words

# Generic fixtures only — no real user names or session content.
_TEST_DISPLAY_NAME = "RecognizedUser"
_TEST_SUMMARY_BULLETS = "- Topic alpha\n- Topic beta"
_TEST_SUMMARY_SINGLE = "- Topic gamma"


class GreetingSummaryTests(unittest.TestCase):
    def test_no_summary_short_greeting(self) -> None:
        prompt = build_greeting_prompt(_TEST_DISPLAY_NAME, is_return_visitor=False)
        self.assertIn(_TEST_DISPLAY_NAME, prompt)
        self.assertIn("1–2 short spoken sentences", prompt)
        self.assertNotIn("Earlier session summary", prompt)

    def test_with_summary_mentions_topics(self) -> None:
        prompt = build_greeting_prompt(
            _TEST_DISPLAY_NAME,
            is_return_visitor=False,
            session_summary=_TEST_SUMMARY_BULLETS,
        )
        self.assertIn("Earlier session summary", prompt)
        self.assertIn("Topic alpha", prompt)
        self.assertIn("max 35 words", prompt)
        self.assertIn("want to continue", prompt)

    def test_return_visitor_flag_preserved(self) -> None:
        prompt = build_greeting_prompt(
            _TEST_DISPLAY_NAME,
            is_return_visitor=True,
            session_summary=_TEST_SUMMARY_SINGLE,
        )
        self.assertIn("returning", prompt.lower())
        self.assertIn("Topic gamma", prompt)


class StartupGreetingTests(unittest.TestCase):
    def test_needs_startup_greeting_until_completed(self) -> None:
        svc = TTSService(cooldown_seconds=999.0, face_greeting_interval_seconds=999.0)
        try:
            self.assertTrue(svc.needs_startup_summary_greeting("RecognizedUser"))
            with svc._lock:
                svc._startup_greeted.add("RecognizedUser")
            self.assertFalse(svc.needs_startup_summary_greeting("RecognizedUser"))
        finally:
            svc.stop()

    def test_first_sight_queues_startup_summary_greeting(self) -> None:
        svc = TTSService(cooldown_seconds=0.0, face_greeting_interval_seconds=999.0)
        try:
            with patch.object(svc, "_ollama_configured", return_value=True):
                with patch("tts_service._pick_include_db_context", return_value=True):
                    svc.update_face_state(
                        ["RecognizedUser"], primary_name="RecognizedUser"
                    )
            with svc._lock:
                self.assertEqual(len(svc._pending_jobs), 1)
                job = svc._pending_jobs[0]
                self.assertTrue(job.is_startup_greeting)
                self.assertTrue(job.include_db_context)
                self.assertEqual(job.llm_name, "RecognizedUser")
        finally:
            svc.stop()

    def test_first_sight_can_queue_plain_greeting(self) -> None:
        svc = TTSService(cooldown_seconds=0.0, face_greeting_interval_seconds=999.0)
        try:
            with patch.object(svc, "_ollama_configured", return_value=True):
                with patch("tts_service._pick_include_db_context", return_value=False):
                    svc.update_face_state(
                        ["RecognizedUser"], primary_name="RecognizedUser"
                    )
            with svc._lock:
                self.assertEqual(len(svc._pending_jobs), 1)
                job = svc._pending_jobs[0]
                self.assertTrue(job.is_startup_greeting)
                self.assertFalse(job.include_db_context)
        finally:
            svc.stop()

    def test_reentry_greeting_randomizes_db_mode(self) -> None:
        svc = TTSService(cooldown_seconds=0.0, face_greeting_interval_seconds=1.0)
        try:
            with patch.object(svc, "_ollama_configured", return_value=True):
                with patch("tts_service._pick_include_db_context", return_value=False):
                    svc.update_face_state(
                        ["RecognizedUser"], primary_name="RecognizedUser"
                    )
                with svc._lock:
                    svc._startup_greeted.add("RecognizedUser")
                    svc._known_seen_once.add("RecognizedUser")
                    svc._pending_jobs.clear()
                    svc._vision_queued.clear()
                    svc._present_known_names.clear()
                    # Welcome-back needs a prior speak older than the interval.
                    svc._last_spoken_at["RecognizedUser"] = time.time() - 10.0
                with patch("tts_service._pick_include_db_context", return_value=True):
                    svc.update_face_state(
                        ["RecognizedUser"], primary_name="RecognizedUser"
                    )
            with svc._lock:
                self.assertEqual(len(svc._pending_jobs), 1)
                job = svc._pending_jobs[0]
                self.assertFalse(job.is_startup_greeting)
                self.assertTrue(job.include_db_context)
                self.assertTrue(job.llm_return_visitor)
        finally:
            svc.stop()

    def test_voice_does_not_skip_startup_summary_greeting(self) -> None:
        svc = TTSService(cooldown_seconds=0.0, face_greeting_interval_seconds=999.0)
        try:
            svc.notify_voice_interaction("RecognizedUser")
            self.assertTrue(svc.needs_startup_summary_greeting("RecognizedUser"))
            # Past voice suppress window
            with svc._lock:
                svc._suppress_vision_until = 0.0
            with patch.object(svc, "_ollama_configured", return_value=True):
                with patch("tts_service._pick_include_db_context", return_value=True):
                    svc.update_face_state(
                        ["RecognizedUser"], primary_name="RecognizedUser"
                    )
            with svc._lock:
                self.assertTrue(svc._pending_jobs)
                self.assertTrue(svc._pending_jobs[0].is_startup_greeting)
        finally:
            svc.stop()

    def test_startup_prompt_excludes_mood(self) -> None:
        prompt = build_greeting_prompt(
            _TEST_DISPLAY_NAME,
            is_return_visitor=False,
            session_summary=_TEST_SUMMARY_SINGLE,
            is_startup_greeting=True,
        )
        self.assertIn("Do NOT mention facial expression", prompt)

    def test_parse_summary_skips_preferences(self) -> None:
        summary = (
            "- User learned about topic alpha and their uses.\n"
            "- User received names of topic beta.\n"
            "- User preferred item A over item B for beverages.\n"
            "- The conversation shifted to discussing gaming preferences."
        )
        topics = parse_summary_topics_for_greeting(summary)
        self.assertEqual(topics[0], "topic alpha and their uses")
        self.assertIn("topic beta", topics[1])
        self.assertEqual(len(topics), 2)

    def test_startup_opener_format(self) -> None:
        hello = build_startup_greeting_hello(_TEST_DISPLAY_NAME)
        yesterday = build_startup_greeting_yesterday("topic alpha and their uses")
        self.assertEqual(hello, "Hi RecognizedUser, good to see you!")
        self.assertEqual(
            yesterday,
            "Yesterday we discussed topic alpha and their uses.",
        )

    def test_closing_question_prompt_includes_both_styles(self) -> None:
        hello = build_startup_greeting_hello(_TEST_DISPLAY_NAME)
        yesterday = build_startup_greeting_yesterday("topic alpha")
        prompt = build_startup_closing_question_prompt(
            _TEST_DISPLAY_NAME,
            "topic alpha",
            hello,
            yesterday,
            "- User learned about topic alpha.",
        )
        self.assertIn("Want to pick up where we left off?", prompt)
        self.assertIn("Ask a simple follow-up about topic alpha", prompt)
        self.assertIn("about topic alpha and nothing else", prompt)
        self.assertIn(hello, prompt)
        self.assertIn(yesterday, prompt)

    def test_startup_llm_prompt_structure_fallback(self) -> None:
        summary = (
            "- User learned about topic alpha and their uses.\n"
            "- User received names of topic beta."
        )
        prompt = build_startup_summary_greeting_prompt(_TEST_DISPLAY_NAME, summary)
        self.assertIn(_TEST_DISPLAY_NAME, prompt)
        self.assertIn("topic alpha and their uses", prompt)
        self.assertIn("SAME single topic", prompt)
        self.assertIn("Yesterday we discussed", prompt)

    def test_clamp_preserves_invitation_question(self) -> None:
        long = (
            "Hi RecognizedUser, good to see you again today after the server started "
            "and we had a long chat about topic alpha and topic beta and more details "
            "from yesterday's session. Want to pick up from there?"
        )
        out = _clamp_spoken_words(long, max_words=25, preserve_invite=True)
        self.assertTrue(out.endswith("?"))
        self.assertIn("pick up", out.lower())

    @patch("llm_service.ollama_generate")
    def test_startup_greeting_structured(self, mock_generate) -> None:
        mock_generate.return_value = "Want to pick up from there?"
        summary = "- User learned about topic alpha and their uses."
        text = startup_greeting_from_summary(
            _TEST_DISPLAY_NAME,
            summary,
            model="test-model",
            api_url="http://127.0.0.1:11435/api/generate",
        )
        expected = (
            "Hi RecognizedUser, good to see you! Yesterday we discussed "
            "topic alpha and their uses. Want to pick up from there?"
        )
        self.assertEqual(text, expected)
        mock_generate.assert_called_once()
        prompt = mock_generate.call_args[0][0]
        self.assertIn("Want to pick up where we left off?", prompt)
        self.assertIn("Ask a simple follow-up about topic alpha", prompt)

    def test_sanitize_strips_greeting_leak(self) -> None:
        raw = (
            "Hi RecognizedUser, good to see you. "
            "Can you tell me the different microcontrollers?"
        )
        clean = _sanitize_closing_question(raw, _TEST_DISPLAY_NAME)
        self.assertEqual(clean, "Can you tell me the different microcontrollers?")

    @patch("llm_service.ollama_generate")
    def test_startup_parts_three_sentences(self, mock_generate) -> None:
        mock_generate.return_value = "Can you tell me the different microcontrollers?"
        summary = "- User learned about microcontrollers and their uses."
        parts = startup_greeting_parts_from_summary(
            _TEST_DISPLAY_NAME,
            summary,
            model="test-model",
            api_url="http://127.0.0.1:11435/api/generate",
        )
        assert parts is not None
        self.assertIn("Yesterday we discussed microcontrollers", parts.yesterday)
        self.assertNotIn("good to see you", parts.question.lower())
        spoken = parts.spoken()
        self.assertIn("Yesterday we discussed", spoken)
        self.assertTrue(spoken.endswith("?"))

    @patch("llm_service.ollama_generate")
    def test_startup_greeting_topic_follow_up(self, mock_generate) -> None:
        mock_generate.return_value = "Can you describe what topic alpha is?"
        summary = "- User learned about topic alpha and their uses."
        text = startup_greeting_from_summary(
            _TEST_DISPLAY_NAME,
            summary,
            model="test-model",
            api_url="http://127.0.0.1:11435/api/generate",
        )
        self.assertTrue(text.endswith("?"))
        self.assertIn("Yesterday we discussed topic alpha and their uses.", text)
        self.assertIn("describe", text.lower())

    @patch("llm_service.ollama_generate")
    def test_startup_greeting_fallback_when_no_closing(self, mock_generate) -> None:
        mock_generate.return_value = ""
        summary = "- User learned about topic alpha."
        text = startup_greeting_from_summary(
            _TEST_DISPLAY_NAME,
            summary,
            model="test-model",
            api_url="http://127.0.0.1:11435/api/generate",
        )
        self.assertIn("Want to pick up from there?", text)
        self.assertIn("topic alpha.", text)

    def test_finalize_keeps_existing_question(self) -> None:
        text = "Hi there! Yesterday we discussed topic alpha. Ready to continue?"
        self.assertEqual(
            finalize_startup_greeting(text, _TEST_DISPLAY_NAME),
            text,
        )


if __name__ == "__main__":
    unittest.main()
