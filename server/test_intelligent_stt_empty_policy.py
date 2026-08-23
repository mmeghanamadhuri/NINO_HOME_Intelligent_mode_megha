"""STT-empty detection policy — repeat threshold, verification, email."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from intelligent_mode.detectors import DetectionCandidate
from intelligent_mode.incident_filters import should_email_incident, should_open_incident
from intelligent_mode.incidents import FixAttempt, Incident
from intelligent_mode.reporter import build_report
from intelligent_mode.workers import verify_incident_cleared


class SttEmptyPolicyTests(unittest.TestCase):
    def _snapshot(self) -> dict:
        return {
            "llm": {"reachable": True, "loaded": True, "base_url": "http://127.0.0.1:11435"},
            "stt": {"provider": "whisper", "loaded": True},
            "devices": {"devices": []},
        }

    def test_single_stt_empty_does_not_open_incident(self) -> None:
        cand = DetectionCandidate(
            device_id="b0a6048addd4",
            display_name="Robot",
            subsystem="voice",
            severity="warning",
            tier=1,
            error="No speech recognized from input audio.",
            snapshot_hint={},
        )
        with patch(
            "intelligent_mode.voice_incident_filters.should_open_stt_empty_incident",
            return_value=False,
        ):
            self.assertFalse(should_open_incident(cand, self._snapshot()))

        with patch(
            "intelligent_mode.voice_incident_filters.should_open_stt_empty_incident",
            return_value=True,
        ):
            self.assertTrue(should_open_incident(cand, self._snapshot()))

    def test_resolved_stt_without_fix_is_not_emailed(self) -> None:
        inc = Incident(
            device_id="b0a6048addd4",
            display_name="Robot",
            subsystem="voice",
            severity="warning",
            tier=1,
            error="No speech recognized from input audio.",
            signature="sig",
            status="resolved",
            debug_report={
                "agent_remediation": {
                    "pattern_id": "voice_stt_recovery",
                    "recovery_actions": ["voice_pipeline_recovery"],
                }
            },
        )
        self.assertFalse(should_email_incident(inc))

    def test_resolved_stt_with_fix_is_emailed(self) -> None:
        inc = Incident(
            device_id="b0a6048addd4",
            display_name="Robot",
            subsystem="voice",
            severity="warning",
            tier=1,
            error="No speech recognized from input audio.",
            signature="sig",
            status="resolved",
            fixes=[FixAttempt(action="voice_pipeline_recovery", success=True, detail="ok")],
            debug_report={
                "agent_remediation": {
                    "pattern_id": "voice_stt_recovery",
                    "recovery_actions": ["voice_pipeline_recovery"],
                }
            },
        )
        self.assertTrue(should_email_incident(inc))

    def test_verification_fails_while_recent_stt_empty_in_log(self) -> None:
        inc = Incident(
            device_id="b0a6048addd4",
            display_name="Robot",
            subsystem="voice",
            severity="warning",
            tier=1,
            error="No speech recognized from input audio.",
            signature="sig",
        )
        with patch(
            "intelligent_mode.voice_incident_filters.device_has_recent_stt_empty",
            return_value=True,
        ):
            self.assertFalse(verify_incident_cleared(inc, self._snapshot()))

    def test_headline_not_auto_fixed_without_recovery(self) -> None:
        inc = Incident(
            device_id="b0a6048addd4",
            display_name="Robot ninohome",
            subsystem="voice",
            severity="warning",
            tier=1,
            error="No speech recognized from input audio.",
            signature="sig",
            status="resolved",
            debug_report={
                "agent_remediation": {"pattern_id": "voice_stt_recovery"},
                "category": "operational",
            },
        )
        report = build_report(inc)
        self.assertIn("speech not recognized", report.lower())
        self.assertNotIn("auto-fixed", report.lower())


if __name__ == "__main__":
    unittest.main()
