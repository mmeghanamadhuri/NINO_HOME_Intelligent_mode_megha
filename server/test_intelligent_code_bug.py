import json
import unittest
from pathlib import Path
from unittest.mock import patch

from intelligent_mode.code_bug_analyzer import (
    CodeBugAnalysis,
    analyze_code_bug,
    merge_into_debug_report,
)
from intelligent_mode.incidents import Incident
from intelligent_mode.reporter import _build_plain_report, _headline, email_subject


class CodeBugAnalyzerTests(unittest.TestCase):
    def _incident(self, **overrides: object) -> Incident:
        base = {
            "device_id": "b0a6048addd4",
            "display_name": "Robot",
            "subsystem": "voice",
            "severity": "warning",
            "tier": 1,
            "error": "No speech recognized from input audio.",
            "signature": "b0:voice:empty",
            "status": "escalated",
        }
        base.update(overrides)
        return Incident(**base)  # type: ignore[arg-type]

    def test_detects_stt_empty_code_bug(self) -> None:
        rows = [
            {
                "event": "voice_query",
                "device_id": "b0a6048addd4",
                "reply_path": "wake_reject",
                "error": "No speech recognized from input audio.",
            }
        ]
        with patch(
            "intelligent_mode.code_bug_analyzer._load_recent_voice_rows",
            return_value=rows,
        ):
            analysis = analyze_code_bug(self._incident(), use_llm=False)
        self.assertFalse(analysis.is_code_bug)

    def test_stt_empty_is_agent_remediatable(self) -> None:
        from intelligent_mode.agent_remediation import is_agent_remediatable_incident
        from intelligent_mode.code_bug_analyzer import is_code_bug_incident

        inc = self._incident(error="No speech recognized from input audio.")
        self.assertTrue(is_agent_remediatable_incident(inc))
        self.assertFalse(is_code_bug_incident(inc))

    def test_merge_into_debug_report(self) -> None:
        analysis = CodeBugAnalysis(
            is_code_bug=True,
            bug_summary="STT empty on bot",
            suggested_fix="Fix mic path",
            firmware_update_recommended=True,
        )
        merged = merge_into_debug_report({"category": "operational"}, analysis)
        self.assertEqual(merged["category"], "logic_bug")
        self.assertFalse(merged["fixable_by_agent"])
        self.assertTrue(merged["code_bug"]["is_code_bug"])

    def test_email_includes_code_bug_sections(self) -> None:
        incident = self._incident(
            subsystem="camera",
            error="Camera regression in firmware session gate during voice",
            debug_report={
                "category": "logic_bug",
                "root_cause": "Camera firmware regression",
                "fixable_by_agent": False,
                "code_bug": {
                    "is_code_bug": True,
                    "id": "camera_session",
                    "bug_summary": "Camera stream fails during an active voice session.",
                    "likely_cause": "UVC host timing in firmware",
                    "suggested_fix": "Review main/ UVC camera code and rebuild firmware.",
                    "affected_files": ["main/"],
                    "firmware_update_recommended": True,
                    "firmware_filename": "demo.bin",
                },
            },
        )
        report = _build_plain_report(incident)
        self.assertIn("CODE BUG ANALYSIS", report)
        self.assertIn("Bot: Robot", report)
        self.assertIn("Device ID: b0a6048addd4", report)
        self.assertIn("THE PROBLEM — Robot", report)
        self.assertIn("LIKELY CAUSE — Robot", report)
        self.assertIn("SUGGESTED FIX (code change) — Robot", report)
        self.assertIn("AFFECTED FILES — Robot", report)
        self.assertIn("FIRMWARE UPDATE — Robot", report)
        self.assertIn("/api/ota/deploy/b0a6048addd4", report)
        self.assertIn("Code bug", email_subject(incident))

    def test_detects_wav_too_large_code_bug(self) -> None:
        analysis = analyze_code_bug(
            self._incident(
                error="[soak:soak:voice:live:1:6:tell_me_a_fun_fact] ESP play_wav failed: WAV too large for ESP (463798 bytes; max 389120)"
            ),
            use_llm=False,
        )
        self.assertFalse(analysis.is_code_bug)

    def test_is_code_bug_incident_from_error(self) -> None:
        from intelligent_mode.code_bug_analyzer import is_code_bug_incident

        inc = self._incident(
            error="ESP play_wav failed: WAV too large for ESP (463798 bytes; max 389120)"
        )
        self.assertFalse(is_code_bug_incident(inc))

    def test_soak_valid_reply_is_not_code_bug(self) -> None:
        from intelligent_mode.code_bug_analyzer import is_code_bug_incident

        inc = self._incident(
            error=(
                "[soak:soak:voice:live:1:5:tell_me_a_joke] path=joke unexpected reply="
                "Ha, okay, here we go! I told my computer I needed a break."
            ),
            debug_report={
                "category": "logic_bug",
                "fixable_by_agent": False,
            },
        )
        self.assertFalse(is_code_bug_incident(inc))

    def test_soak_empathetic_llm_reply_overrides_stale_code_bug_flag(self) -> None:
        from intelligent_mode.code_bug_analyzer import is_code_bug_incident

        inc = self._incident(
            error=(
                "[soak:soak:voice:live:4:7:i_feel_lonely_can_we_talk] path=llm unexpected reply="
                "Absolutely, feeling lonely is a common feeling. Let's chat about anything you'd like."
            ),
            debug_report={
                "category": "logic_bug",
                "fixable_by_agent": False,
                "code_bug": {
                    "is_code_bug": True,
                    "bug_summary": "Voice pipeline receives audio but STT returns empty text",
                },
            },
        )
        self.assertFalse(is_code_bug_incident(inc))
        merged = merge_into_debug_report(inc.debug_report, CodeBugAnalysis(is_code_bug=False))
        self.assertTrue(merged["fixable_by_agent"])
        self.assertNotIn("code_bug", merged)

    def test_code_bug_email_not_marked_resolved(self) -> None:
        incident = self._incident(
            status="escalated",
            error="Camera stream regression in firmware session gate",
            debug_report={
                "category": "logic_bug",
                "fixable_by_agent": False,
                "code_bug": {
                    "is_code_bug": True,
                    "bug_summary": "Camera fails during voice session",
                    "likely_cause": "UVC timing",
                    "suggested_fix": "Review main/ camera code",
                    "affected_files": ["main/"],
                },
            },
        )
        report = _build_plain_report(incident)
        self.assertIn("code bug", _headline(incident).lower())
        self.assertIn("developer fix required", _headline(incident).lower())
        self.assertNotIn("Status:   Resolved", report)
        self.assertIn("Code fix required", report)
        self.assertIn("Code bug", email_subject(incident))

    def test_ota_skipped_when_disabled(self) -> None:
        from intelligent_mode.code_bug_analyzer import try_firmware_ota_for_incident

        analysis = CodeBugAnalysis(
            is_code_bug=True,
            firmware_update_recommended=True,
            firmware_filename="test.bin",
        )
        with patch.dict("os.environ", {"INTELLIGENT_AUTO_OTA": "0"}):
            out = try_firmware_ota_for_incident(self._incident(), analysis)
        self.assertIn("disabled", out.ota_detail.lower())


if __name__ == "__main__":
    unittest.main()
