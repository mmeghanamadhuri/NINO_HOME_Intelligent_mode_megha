import unittest
from unittest.mock import patch

from intelligent_mode.debugger import analyze_incident, build_debug_report
from intelligent_mode.incidents import FixAttempt, Incident


class DebuggerTests(unittest.TestCase):
    def _incident(self, **overrides: object) -> Incident:
        base = {
            "device_id": "server",
            "display_name": "NiNO Server",
            "subsystem": "llm",
            "severity": "critical",
            "tier": 0,
            "error": "Connection refused on 127.0.0.1:11434",
            "signature": "server:llm:down",
            "status": "open",
        }
        base.update(overrides)
        return Incident(**base)  # type: ignore[arg-type]

    def test_operational_llm_analysis(self) -> None:
        report = analyze_incident(
            self._incident(),
            snapshot={"llm": {"reachable": False, "loaded": False}},
            use_llm=False,
        )
        self.assertEqual(report.category, "operational")
        self.assertIn("Ollama", report.root_cause)
        self.assertTrue(report.fixable_by_agent)

    def test_not_fixable_after_repeated_failures(self) -> None:
        incident = self._incident(
            status="escalated",
            fixes=[
                FixAttempt("ollama_restart_warm", False, "still down"),
                FixAttempt("ollama_restart_warm", False, "still down"),
            ],
        )
        report = analyze_incident(incident, use_llm=False)
        self.assertFalse(report.fixable_by_agent)
        self.assertTrue(any("human investigation" in a.lower() for a in report.suggested_actions))

    def test_voice_logic_bug_from_latency(self) -> None:
        incident = self._incident(
            subsystem="voice",
            error="e2e voice pipeline failed",
            signature="server:voice:fail",
        )
        records = [
            {
                "event": "voice_query",
                "device_id": "server",
                "reply_path": "llm_error",
                "error": "LLM timeout",
            }
        ]
        with patch("intelligent_mode.debugger._load_latency_records", return_value=records):
            report = analyze_incident(incident, use_llm=False)
        self.assertEqual(report.category, "logic_bug")
        self.assertFalse(report.fixable_by_agent)

    def test_build_debug_report_dict(self) -> None:
        payload = build_debug_report(self._incident(), use_llm=False)
        self.assertEqual(payload["category"], "operational")
        self.assertIn("root_cause", payload)


if __name__ == "__main__":
    unittest.main()
