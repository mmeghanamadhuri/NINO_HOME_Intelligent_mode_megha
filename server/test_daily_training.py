"""Tests for nightly daily training scheduler."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

from daily_training_service import (
    DailyTrainingService,
    load_last_run,
    run_daily_training,
)


class DailyTrainingRunTests(unittest.TestCase):
    def test_run_daily_training_face_and_memory(self) -> None:
        faces = MagicMock()
        faces.faces_dir = Path("/tmp/faces")
        faces.train.return_value = {"people": 2, "samples": 40}

        memory = MagicMock()
        memory.ready = True
        memory.run_daily_extraction_batch.return_value = {
            "ok": True,
            "day": "2026-08-22",
            "total_turns": 5,
            "turns_processed": 3,
        }
        memory.export_training_day.return_value = {
            "ok": True,
            "rows": 5,
            "daily_file": "/tmp/daily.jsonl",
        }

        with patch.object(Path, "glob", return_value=["a.jpg"]):
            with patch("daily_training_service._save_last_run"):
                result = run_daily_training(
                    faces=faces,
                    memory_service=memory,
                    target_day=date(2026, 8, 22),
                )

        self.assertTrue(result["ok"])
        self.assertEqual(result["face"]["people"], 2)
        faces.train.assert_called_once()
        memory.run_daily_extraction_batch.assert_called_once_with(date(2026, 8, 22))
        memory._run_summary_catchup_safe.assert_called_once()
        memory.export_training_day.assert_called_once()

    def test_run_daily_training_skips_face_without_samples(self) -> None:
        faces = MagicMock()
        faces.faces_dir = Path("/tmp/empty")
        memory = MagicMock()
        memory.ready = False

        with patch.object(Path, "glob", return_value=[]):
            with patch("daily_training_service._save_last_run"):
                result = run_daily_training(
                    faces=faces,
                    memory_service=memory,
                    target_day=date(2026, 8, 22),
                )

        self.assertTrue(result["face"]["skipped"])
        faces.train.assert_not_called()


class DailyTrainingServiceTests(unittest.TestCase):
    def test_status_includes_scheduler_time(self) -> None:
        svc = DailyTrainingService()
        with patch.dict("os.environ", {"DAILY_TRAINING": "1", "DAILY_TRAINING_TIME": "02:00"}):
            status = svc.status()
        self.assertTrue(status["enabled"])
        self.assertEqual(status["scheduler_time"], "02:00")

    def test_load_last_run_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch("daily_training_service._LAST_RUN_PATH", Path(tmp) / "missing.json"):
                self.assertIsNone(load_last_run())


if __name__ == "__main__":
    unittest.main()
