"""Regression tests for direct replies to recognized speakers."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from llm_service import (
    _defers_to_recognized_speaker,
    answer_voice_query,
    resolve_ollama_api_url,
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


if __name__ == "__main__":
    unittest.main()
