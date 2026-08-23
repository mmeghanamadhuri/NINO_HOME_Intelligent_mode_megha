"""Unit tests for E2E voice suite and silent recovery."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from intelligent_mode.e2e_voice_test import failures_to_e2e_candidates, run_e2e_voice_suite
from voice_service import voice_error_tts_enabled, voice_pipeline_recovery_wav


class VoiceRecoveryTests(unittest.TestCase):
    def test_silent_recovery_default(self) -> None:
        with patch.dict("os.environ", {"VOICE_ERROR_TTS": "silent"}, clear=False):
            self.assertFalse(voice_error_tts_enabled())
            wav = voice_pipeline_recovery_wav(ConnectionError("ollama down"))
            self.assertGreater(len(wav), 0)

    def test_spoken_recovery_when_enabled(self) -> None:
        with patch.dict("os.environ", {"VOICE_ERROR_TTS": "speak"}, clear=False):
            with patch("tts_service.synthesize_sapi_wav_bytes", return_value=(b"wav", "v")):
                with patch(
                    "wav_resample.resample_wav_bytes_to_mono_16bit",
                    return_value=b"out",
                ):
                    out = voice_pipeline_recovery_wav(ConnectionError("ollama down"))
                    self.assertEqual(out, b"out")


class E2eSuiteTests(unittest.TestCase):
    def test_skips_when_llm_down(self) -> None:
        snap = {"llm": {"reachable": False, "warning": "connection refused"}}
        run = run_e2e_voice_suite(snap)
        self.assertEqual(run.failed, 1)
        self.assertGreater(run.skipped, 0)

    @patch("intelligent_mode.e2e_voice_test._voice_pipeline_check", return_value=(True, "ok"))
    @patch("llm_service.ollama_generate", side_effect=["four", "hello", "blue"])
    def test_runs_questions_when_llm_up(
        self,
        _gen: MagicMock,
        _pipe: MagicMock,
    ) -> None:
        snap = {"llm": {"reachable": True, "loaded": True, "model": "qwen2.5:1.5b"}}
        run = run_e2e_voice_suite(snap)
        self.assertEqual(run.failed, 0)
        self.assertEqual(run.passed, 4)

    def test_failures_become_candidates(self) -> None:
        snap = {"llm": {"reachable": False, "warning": "down"}}
        run = run_e2e_voice_suite(snap)
        cands = failures_to_e2e_candidates(run)
        self.assertEqual(len(cands), 1)
        self.assertIn("e2e:", cands[0].error)


if __name__ == "__main__":
    unittest.main()
