"""Tests for rolling baseline anomaly detection."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from intelligent_mode.baselines import BaselineStore, detect_baseline_anomalies
from intelligent_mode.detectors import GraceTracker


class BaselineTests(unittest.TestCase):
    def test_record_and_anomaly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = BaselineStore(path=Path(tmp) / "baselines.json")
            for i in range(25):
                store.record("bot:voice_total_s", 1.0 + (i % 4) * 0.05)
            is_bad, stats, z = store.is_anomaly("bot:voice_total_s", 8.5, sigma=3.0, min_samples=20)
            self.assertTrue(is_bad)
            self.assertIsNotNone(stats)
            self.assertGreater(z, 3.0)

    def test_detect_baseline_anomalies_with_latency(self) -> None:
        grace = GraceTracker(grace_seconds=0)
        latency_rows = [
            {
                "event": "voice_query",
                "device_id": "bot1",
                "server_total_seconds": 8.0,
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            store = BaselineStore(path=Path(tmp) / "baselines.json")
            for i in range(25):
                store.record("bot1:voice_total_s", 1.0 + (i % 3) * 0.05)
            with patch("intelligent_mode.baselines._recent_latency_rows", return_value=latency_rows):
                found = detect_baseline_anomalies(
                    {"devices": {"devices": [{"device_id": "bot1", "display_name": "Bot"}]}},
                    grace=grace,
                    store=store,
                    sigma=3.0,
                    min_samples=20,
                    grace_seconds=0,
                )
            self.assertEqual(len(found), 1)
            self.assertIn("baseline anomaly", found[0].error.lower())


if __name__ == "__main__":
    unittest.main()
