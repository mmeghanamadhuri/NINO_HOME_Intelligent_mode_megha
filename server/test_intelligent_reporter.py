"""Reporter formatting and email content tests."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from intelligent_mode.incidents import FixAttempt, Incident
from intelligent_mode.reporter import (
    build_digest_html,
    build_digest_report,
    build_html_report,
    build_report,
    email_subject,
)


class ReporterFormatTests(unittest.TestCase):
    def _camera_incident(self) -> Incident:
        return Incident(
            device_id="b0a6048addd4",
            display_name="B0:A6:04:8A:DD:D4",
            subsystem="camera",
            severity="critical",
            tier=0,
            error="HTTP Error 503: Service Unavailable",
            signature="b0a6048addd4:camera:503",
            status="open",
            fixes=[
                FixAttempt(
                    action="camera_restart",
                    success=False,
                    detail="[b0a6048addd4] restarted http://192.168.1.148/stream; connected=False",
                )
            ],
            debug_report={
                "category": "operational",
                "root_cause": "ESP camera endpoint returned 503 — UVC stream may be busy or still enumerating.",
                "confidence": "medium",
                "fixable_by_agent": True,
                "suggested_actions": [
                    "Wait for USB hub enumeration to finish, then retry.",
                    "Check ESP serial logs for UVC/camera errors.",
                ],
            },
        )

    def test_subject_is_human_readable(self) -> None:
        subject = email_subject(self._camera_incident())
        self.assertIn("[NiNO]", subject)
        self.assertIn("Camera", subject)
        self.assertNotIn("HTTP Error 503", subject)

    def test_plain_report_has_clear_sections(self) -> None:
        text = build_report(self._camera_incident(), use_llm=False)
        self.assertIn("NiNO Intelligent Mode — Testing & Recovery Agent", text)
        self.assertIn("IN PLAIN ENGLISH", text)
        self.assertIn("WHAT HAPPENED", text)
        self.assertIn("WHAT INTELLIGENT MODE TRIED", text)
        self.assertIn("WHAT TO DO NEXT", text)
        self.assertIn("TECHNICAL DETAILS", text)
        self.assertIn("HTTP Error 503", text)
        self.assertNotIn("LLM note:", text)

    def test_agent_handled_wav_email_is_clear(self) -> None:
        inc = Incident(
            device_id="b0a6048addd4",
            display_name="Robot",
            subsystem="voice",
            severity="critical",
            tier=0,
            error="ESP play_wav failed: WAV too large for ESP (800370 bytes; max 389120)",
            signature="b0:voice:wav",
            status="resolved",
            fixes=[
                FixAttempt(
                    action="voice_pipeline_recovery",
                    success=True,
                    detail="pipeline ok",
                )
            ],
        )
        text = build_report(inc, use_llm=False)
        self.assertIn("IN PLAIN ENGLISH", text)
        self.assertIn("HANDLED BY INTELLIGENT MODE", text)
        self.assertIn("No action needed", text)
        self.assertIn("splits long audio", text.lower())
        self.assertIn("Auto-fixed", email_subject(inc))
        self.assertNotIn("developer fix required", text.lower())

    def test_agent_handled_stt_email_is_clear(self) -> None:
        inc = Incident(
            device_id="b0a6048addd4",
            display_name="Robot",
            subsystem="voice",
            severity="critical",
            tier=0,
            error="STT path=stt_empty",
            signature="b0:voice:stt",
            status="fixing",
        )
        text = build_report(inc, use_llm=False)
        self.assertIn("speech-to-text", text.lower())
        self.assertIn("Intelligent Mode is fixing this", text)
        self.assertIn("Auto-fix in progress", email_subject(inc))

    def test_agent_handled_soak_reply_email_is_clear(self) -> None:
        inc = Incident(
            device_id="b0a6048addd4",
            display_name="Robot",
            subsystem="voice",
            severity="warning",
            tier=1,
            error=(
                "[soak:voice] path=llm unexpected reply="
                "Absolutely, feeling lonely is common. Let's chat."
            ),
            signature="b0:voice:soak",
            status="resolved",
        )
        text = build_report(inc, use_llm=False)
        self.assertIn("false alarm", text.lower())
        self.assertIn("No action needed", text)
        html = build_html_report(inc)
        self.assertIn("Handled by Intelligent Mode", html)

    def test_plain_report_explains_camera_503_in_plain_english(self) -> None:
        text = build_report(self._camera_incident(), use_llm=False)
        self.assertIn("camera could not provide a snapshot", text.lower())

    def test_html_report_includes_structure(self) -> None:
        html = build_html_report(self._camera_incident())
        self.assertIn("<!DOCTYPE html>", html)
        self.assertIn("What to do next", html)
        self.assertIn("Needs attention", html)

    def test_digest_report_lists_incidents_clearly(self) -> None:
        text = build_digest_report([self._camera_incident()])
        self.assertIn("NiNO Intelligent Mode Digest", text)
        self.assertIn("need your attention", text)
        self.assertIn("Camera issue", text)

    def test_digest_all_resolved_says_no_action_needed(self) -> None:
        inc = Incident(
            device_id="b0a6048addd4",
            display_name="Robot",
            subsystem="voice",
            severity="critical",
            tier=0,
            error="connection refused",
            signature="b0:voice:ollama",
            status="resolved",
            fixes=[FixAttempt(action="voice_pipeline_recovery", success=True, detail="ok")],
        )
        text = build_digest_report([inc, inc])
        self.assertIn("no action needed", text.lower())
        html = build_digest_html([inc])
        self.assertIn("no action needed", html.lower())
        self.assertNotIn("require your attention", html.lower())

    def test_template_when_ollama_down(self) -> None:
        inc = Incident(
            device_id="server",
            display_name="Server",
            subsystem="llm",
            severity="critical",
            tier=0,
            error="connection refused",
            signature="server:llm:connection refused",
        )
        with patch("llm_service.ollama_is_reachable", return_value=False):
            text = build_report(inc, use_llm=True)
        self.assertIn("NiNO Intelligent Mode", text)
        self.assertIn("not running or cannot be reached", text.lower())


if __name__ == "__main__":
    unittest.main()
