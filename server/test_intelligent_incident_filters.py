"""Tests for central incident detection and email job rules."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from intelligent_mode.incident_filters import (
    is_test_skip_message,
    should_email_incident,
    should_suppress_incident,
    silent_email_pattern_id,
)
from intelligent_mode.incidents import FixAttempt, Incident
from intelligent_mode.smoke_tests import SmokeTestResult, failures_to_candidates, SmokeTestRun


class IncidentFilterJobRulesTests(unittest.TestCase):
    def _snapshot_gpu_up(self) -> dict:
        return {
            "llm": {
                "reachable": True,
                "loaded": True,
                "base_url": "http://127.0.0.1:11435",
            }
        }

    def test_skip_messages_not_incidents(self) -> None:
        msgs = [
            "skipped — live voice session active on bot/server",
            "Ollama unreachable — E2E skipped",
            "LLM unreachable — voice soak skipped",
        ]
        for msg in msgs:
            self.assertTrue(is_test_skip_message(msg), msg)
            self.assertTrue(
                should_suppress_incident(f"[smoke:test] {msg}", "voice", self._snapshot_gpu_up())
            )

    def test_cpu_ollama_suppressed_when_gpu_up(self) -> None:
        err = (
            "HTTPConnectionPool(host='127.0.0.1', port=11434): Max retries exceeded "
            "with url: /api/generate"
        )
        self.assertTrue(should_suppress_incident(err, "voice", self._snapshot_gpu_up()))

    def test_smoke_failures_skip_dependency_skips(self) -> None:
        run = SmokeTestRun(
            results=[
                SmokeTestResult(
                    test_id="server:ollama_model_loaded",
                    name="server:ollama_model_loaded",
                    device_id="server",
                    subsystem="llm",
                    passed=False,
                    message="skipped — Ollama unreachable",
                    severity="critical",
                    tier=0,
                )
            ]
        )
        self.assertEqual(failures_to_candidates(run), [])

    def test_silent_email_for_benign_patterns(self) -> None:
        inc = Incident(
            device_id="b0a6048addd4",
            display_name="Robot",
            subsystem="voice",
            severity="warning",
            tier=1,
            error=(
                "HTTPConnectionPool(host='127.0.0.1', port=11434): Max retries exceeded "
                "with url: /api/generate"
            ),
            signature="sig",
            status="resolved",
        )
        self.assertEqual(silent_email_pattern_id(inc), "ollama_cpu_optional")
        self.assertFalse(should_email_incident(inc))

    def test_email_not_sent_for_stt_empty_without_fix(self) -> None:
        inc = Incident(
            device_id="b0a6048addd4",
            display_name="Robot",
            subsystem="voice",
            severity="critical",
            tier=0,
            error="STT path=stt_empty",
            signature="sig",
            status="resolved",
        )
        self.assertFalse(should_email_incident(inc))

    def test_email_sent_for_stt_empty_after_recovery_fix(self) -> None:
        inc = Incident(
            device_id="b0a6048addd4",
            display_name="Robot",
            subsystem="voice",
            severity="critical",
            tier=0,
            error="STT path=stt_empty",
            signature="sig",
            status="resolved",
            fixes=[FixAttempt(action="voice_pipeline_recovery", success=True, detail="ok")],
        )
        self.assertTrue(should_email_incident(inc))

    def test_dedupe_smoke_llm_when_detector_present(self) -> None:
        from intelligent_mode.detectors import DetectionCandidate
        from intelligent_mode.incident_filters import dedupe_detection_candidates

        live = DetectionCandidate(
            device_id="server",
            display_name="NiNO Server",
            subsystem="llm",
            severity="critical",
            tier=0,
            error="Ollama status check failed",
            snapshot_hint={},
        )
        smoke = DetectionCandidate(
            device_id="server",
            display_name="NiNO Server",
            subsystem="llm",
            severity="critical",
            tier=0,
            error="[smoke:server:ollama_reachable] unreachable",
            snapshot_hint={},
        )
        out = dedupe_detection_candidates([live, smoke])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].error, "Ollama status check failed")

    def test_should_not_open_benign_cpu_ollama(self) -> None:
        from intelligent_mode.detectors import DetectionCandidate
        from intelligent_mode.incident_filters import should_open_incident

        cand = DetectionCandidate(
            device_id="b0a6048addd4",
            display_name="Robot",
            subsystem="voice",
            severity="warning",
            tier=1,
            error=(
                "HTTPConnectionPool(host='127.0.0.1', port=11434): Max retries exceeded "
                "with url: /api/generate"
            ),
            snapshot_hint={},
        )
        self.assertFalse(
            should_open_incident(cand, self._snapshot_gpu_up())
        )

    def test_real_camera_error_still_opens(self) -> None:
        from intelligent_mode.detectors import DetectionCandidate
        from intelligent_mode.incident_filters import should_open_incident

        cand = DetectionCandidate(
            device_id="b0a6048addd4",
            display_name="Robot",
            subsystem="camera",
            severity="critical",
            tier=0,
            error="HTTP Error 503: Service Unavailable",
            snapshot_hint={},
        )
        self.assertTrue(should_open_incident(cand, self._snapshot_gpu_up()))

    def test_stt_empty_requires_repeat_before_open(self) -> None:
        from intelligent_mode.detectors import DetectionCandidate
        from intelligent_mode.incident_filters import should_open_incident

        cand = DetectionCandidate(
            device_id="b0a6048addd4",
            display_name="Robot",
            subsystem="voice",
            severity="critical",
            tier=0,
            error="STT path=stt_empty",
            snapshot_hint={},
        )
        with patch(
            "intelligent_mode.voice_incident_filters.should_open_stt_empty_incident",
            return_value=False,
        ):
            self.assertFalse(should_open_incident(cand, self._snapshot_gpu_up()))
        with patch(
            "intelligent_mode.voice_incident_filters.should_open_stt_empty_incident",
            return_value=True,
        ):
            self.assertTrue(should_open_incident(cand, self._snapshot_gpu_up()))


if __name__ == "__main__":
    unittest.main()
