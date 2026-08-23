"""Tests for false-positive voice incident suppression."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from intelligent_mode.agent_remediation import classify_agent_remediation
from intelligent_mode.detectors import _recent_voice_failures
from intelligent_mode.incidents import Incident
from intelligent_mode.soak_test import soak_failures_to_candidates, stale_soak_incident_resolution
from intelligent_mode.smoke_tests import SmokeTestResult
from intelligent_mode.voice_incident_filters import (
    is_benign_voice_latency_row,
    is_ollama_cpu_port_error,
    is_suppressed_voice_error,
    should_suppress_voice_incident,
)


class VoiceIncidentFilterTests(unittest.TestCase):
    def _snapshot_gpu_up(self) -> dict:
        return {
            "llm": {
                "reachable": True,
                "loaded": True,
                "base_url": "http://127.0.0.1:11435",
            }
        }

    def test_ollama_cpu_port_error_detected(self) -> None:
        err = (
            "Network request failed (HTTPConnectionPool(host='127.0.0.1', port=11434): "
            "Max retries exceeded with url: /api/generate"
        )
        self.assertTrue(is_ollama_cpu_port_error(err))

    def test_suppress_cpu_error_when_gpu_healthy(self) -> None:
        err = (
            "Network request failed (HTTPConnectionPool(host='127.0.0.1', port=11434): "
            "Max retries exceeded with url: /api/generate"
        )
        snap = self._snapshot_gpu_up()
        self.assertTrue(is_suppressed_voice_error(err, snap))
        self.assertTrue(should_suppress_voice_incident(err, "voice", snap))

    def test_do_not_suppress_cpu_error_when_gpu_down(self) -> None:
        err = (
            "Network request failed (HTTPConnectionPool(host='127.0.0.1', port=11434): "
            "Max retries exceeded with url: /api/generate"
        )
        snap = {"llm": {"reachable": False}}
        self.assertFalse(is_suppressed_voice_error(err, snap))

    def test_wake_reject_latency_row_is_benign(self) -> None:
        row = {"event": "voice_query", "reply_path": "wake_reject", "device_id": "b0"}
        self.assertTrue(is_benign_voice_latency_row(row, self._snapshot_gpu_up()))

    def test_agent_auto_resolves_cpu_ollama_noise(self) -> None:
        inc = Incident(
            device_id="b0a6048addd4",
            display_name="Robot",
            subsystem="voice",
            severity="warning",
            tier=1,
            error=(
                "Network request failed (HTTPConnectionPool(host='127.0.0.1', port=11434): "
                "Max retries exceeded with url: /api/generate"
            ),
            signature="sig",
        )
        plan = classify_agent_remediation(inc)
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.pattern_id, "ollama_cpu_optional")
        self.assertEqual(plan.recovery_actions, ())

    def test_soak_skip_not_a_failure_candidate(self) -> None:
        results = [
            SmokeTestResult(
                test_id="soak:voice:live:1:1:q",
                name="soak:voice:live:1:1:q",
                device_id="b0a6048addd4",
                subsystem="voice",
                passed=False,
                message="skipped — live voice session active on bot/server",
                skipped=True,
                severity="critical",
                tier=0,
            )
        ]
        self.assertEqual(soak_failures_to_candidates(results), [])

    def test_stale_resolution_for_live_session_skip(self) -> None:
        reason = stale_soak_incident_resolution(
            "[soak:soak:voice:live:23:8:cancel_all_my_alarms] "
            "skipped — live voice session active on bot/server"
        )
        self.assertIsNotNone(reason)

    @patch("intelligent_mode.detectors._LATENCY_PATH")
    @patch("intelligent_mode.detectors.json.loads")
    def test_recent_voice_failures_filters_cpu_noise(
        self, mock_loads: object, mock_path: object
    ) -> None:
        rows = [
            {
                "event": "voice_query",
                "device_id": "b0a6048addd4",
                "error": (
                    "HTTPConnectionPool(host='127.0.0.1', port=11434): "
                    "Max retries exceeded with url: /api/generate"
                ),
            }
        ]
        mock_path.is_file.return_value = True
        mock_loads.return_value = rows
        failures = _recent_voice_failures(snapshot=self._snapshot_gpu_up(), limit=5)
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
