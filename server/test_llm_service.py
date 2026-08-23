"""Regression tests for direct replies to recognized speakers."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from llm_service import (
    DEFAULT_LLM_GREETING_ONE_IN,
    DEFAULT_OLLAMA_HTTP_RETRIES,
    _defers_to_recognized_speaker,
    _ollama_http_retries,
    answer_voice_query,
    greeting_allowed_for_llm_turn,
    llm_greeting_one_in,
    resolve_ollama_api_url,
    strip_leading_greeting,
)


class VoiceReplyTests(unittest.TestCase):
    @patch("llm_service.ollama_model_available", side_effect=[False, True])
    def test_stale_explicit_endpoint_falls_back_to_gpu_model(
        self, model_available
    ) -> None:
        cpu_url = "http://127.0.0.1:11434/api/generate"
        gpu_url = "http://127.0.0.1:11435/api/generate"

        with patch.dict("os.environ", {"OLLAMA_GPU_URL": gpu_url}, clear=False):
            resolved = resolve_ollama_api_url(
                model="qwen2.5:1.5b",
                preferred=cpu_url,
            )

        self.assertEqual(resolved, gpu_url)
        self.assertEqual(model_available.call_count, 2)

    def test_detects_reply_that_defers_to_recognized_speaker(self) -> None:
        self.assertTrue(
            _defers_to_recognized_speaker(
                "You should ask Avery. They might know the answer.",
                "Avery",
            )
        )
        self.assertFalse(
            _defers_to_recognized_speaker(
                "Here is a direct answer to your question.",
                "Avery",
            )
        )

    @patch("llm_service.ollama_generate")
    def test_retries_reply_that_treats_speaker_as_third_party(self, generate) -> None:
        generate.side_effect = [
            "You should ask Avery. They might know the answer.",
            "Here is a direct answer to your question.",
        ]

        reply = answer_voice_query(
            "Can you help me with this?",
            viewer_name="Avery",
        )

        self.assertEqual(
            reply,
            "Here is a direct answer to your question.",
        )
        self.assertEqual(generate.call_count, 2)
        self.assertIn("Answer the user's question directly", generate.call_args_list[1].args[0])

    @patch("llm_service.ollama_generate")
    def test_uses_safe_reply_when_retry_still_defers_to_speaker(self, generate) -> None:
        generate.return_value = "Please ask Avery; Avery might know."

        reply = answer_voice_query(
            "Can you help me with this?",
            viewer_name="Avery",
        )

        self.assertEqual(
            reply,
            "I am sorry, I cannot answer that reliably right now.",
        )
        self.assertEqual(generate.call_count, 2)


class ChatGreetingRateTests(unittest.TestCase):
    def test_default_rate_is_one_in_twenty(self) -> None:
        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop("LLM_GREETING_ONE_IN", None)
            self.assertEqual(llm_greeting_one_in(), 20)
        self.assertEqual(DEFAULT_LLM_GREETING_ONE_IN, 20)

    def test_rate_is_configurable(self) -> None:
        with patch.dict("os.environ", {"LLM_GREETING_ONE_IN": "5"}, clear=False):
            self.assertEqual(llm_greeting_one_in(), 5)
        with patch.dict("os.environ", {"LLM_GREETING_ONE_IN": "nope"}, clear=False):
            self.assertEqual(llm_greeting_one_in(), DEFAULT_LLM_GREETING_ONE_IN)

    def test_greeting_is_rare_but_possible(self) -> None:
        with patch("llm_service.random.randrange", return_value=0):
            self.assertTrue(greeting_allowed_for_llm_turn())
        with patch("llm_service.random.randrange", return_value=7):
            self.assertFalse(greeting_allowed_for_llm_turn())

    def test_strip_leading_greeting(self) -> None:
        for raw, expected in (
            ("Good evening! Paris is the capital of France.", "Paris is the capital of France."),
            ("Hi there, the capital of France is Paris.", "The capital of France is Paris."),
            ("Hello Avery! Mars is the fourth planet.", "Mars is the fourth planet."),
            ("Mars is the fourth planet.", "Mars is the fourth planet."),
            ("Good evening!", "Good evening!"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(strip_leading_greeting(raw), expected)

    @patch("llm_service.ollama_generate")
    def test_chat_reply_drops_greeting_when_not_allowed(self, generate) -> None:
        generate.return_value = "Good evening! Mars is the fourth planet."

        with patch("llm_service.greeting_allowed_for_llm_turn", return_value=False):
            reply = answer_voice_query("Tell me about Mars")

        self.assertEqual(reply, "Mars is the fourth planet.")
        self.assertIn("Do NOT greet at all", generate.call_args.args[0])

    @patch("llm_service.ollama_generate")
    def test_chat_reply_keeps_greeting_when_allowed(self, generate) -> None:
        generate.return_value = "Good evening! Mars is the fourth planet."

        with patch("llm_service.greeting_allowed_for_llm_turn", return_value=True):
            reply = answer_voice_query("Tell me about Mars")

        self.assertEqual(reply, "Good evening! Mars is the fourth planet.")
        self.assertNotIn("Do NOT greet at all", generate.call_args.args[0])


class OllamaUrlTests(unittest.TestCase):
    def test_ollama_base_url_strips_doubled_generate_suffix(self) -> None:
        from llm_service import _ollama_base_url

        base = _ollama_base_url("http://127.0.0.1:11435/api/generate/api/generate")
        self.assertEqual(base, "http://127.0.0.1:11435")

    def test_set_ollama_env_url_normalizes_doubled_suffix(self) -> None:
        from llm_service import _ollama_base_url, set_ollama_env_url

        with patch.dict(
            "os.environ",
            {"OLLAMA_URL": "http://127.0.0.1:11435/api/generate/api/generate"},
            clear=False,
        ):
            url = set_ollama_env_url()
            self.assertEqual(url, "http://127.0.0.1:11435/api/generate")
            self.assertEqual(os.environ["OLLAMA_URL"], url)
            self.assertEqual(_ollama_base_url(url), "http://127.0.0.1:11435")


class OllamaHttpRetryTests(unittest.TestCase):
    def test_default_retry_count(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(_ollama_http_retries(), DEFAULT_OLLAMA_HTTP_RETRIES)

    def test_env_override(self) -> None:
        with patch.dict("os.environ", {"OLLAMA_HTTP_RETRIES": "10"}, clear=False):
            self.assertEqual(_ollama_http_retries(), 10)


if __name__ == "__main__":
    unittest.main()
