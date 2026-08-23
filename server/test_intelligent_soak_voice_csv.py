"""Tests for CSV-backed soak voice question bank."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from intelligent_mode.soak_test import SOAK_VOICE_CORE, pick_soak_voice_questions
from intelligent_mode.soak_voice_questions import (
    infer_expected_keywords,
    load_csv_voice_questions,
    pick_csv_voice_questions,
)


class SoakVoiceCsvTests(unittest.TestCase):
    def test_load_csv_bank(self) -> None:
        path = Path(__file__).resolve().parent / "data" / "voice_assistant_test_questions.csv"
        if not path.is_file():
            self.skipTest("voice_assistant_test_questions.csv not present")
        bank = load_csv_voice_questions(str(path))
        self.assertGreaterEqual(len(bank), 500)
        self.assertEqual(bank[0].question, "What is the capital of France?")

    def test_math_keyword_inference(self) -> None:
        keys = infer_expected_keywords("What is 12 times 8?", "Math and calculations", "")
        self.assertIn("96", keys)

    def test_pick_csv_rotates_categories(self) -> None:
        path = Path(__file__).resolve().parent / "data" / "voice_assistant_test_questions.csv"
        if not path.is_file():
            self.skipTest("voice_assistant_test_questions.csv not present")
        a = pick_csv_voice_questions(cycle_number=1, count=5)
        b = pick_csv_voice_questions(cycle_number=2, count=5)
        self.assertEqual(len(a), 5)
        self.assertEqual(len(b), 5)
        self.assertNotEqual({q for q, _ in a}, {q for q, _ in b})

    def test_pick_soak_voice_questions_uses_csv(self) -> None:
        path = Path(__file__).resolve().parent / "data" / "voice_assistant_test_questions.csv"
        if not path.is_file():
            self.skipTest("voice_assistant_test_questions.csv not present")
        with patch.dict(
            os.environ,
            {
                "SOAK_VOICE_QUESTIONS_CSV": str(path),
                "SOAK_VOICE_ALL_AGES": "0",
                "SOAK_VOICE_QUESTIONS_PER_CYCLE": "8",
            },
            clear=False,
        ):
            picked = pick_soak_voice_questions(cycle_number=10)
        self.assertEqual(len(picked), 8)
        self.assertIn("What is 2 plus 2?", {q for q, _ in picked})
        core_only = {q for q, _ in SOAK_VOICE_CORE}
        csv_questions = [q for q, _ in picked if q not in core_only]
        self.assertGreaterEqual(len(csv_questions), 3)


if __name__ == "__main__":
    unittest.main()
