import unittest

from intelligent_mode.incident_ui import classify_incident_for_ui, enrich_incident_for_ui


class IncidentUiTests(unittest.TestCase):
    def test_soak_false_positive_lonely_reply(self) -> None:
        raw = {
            "device_id": "b0a6048addd4",
            "subsystem": "voice",
            "error": (
                "[soak:soak:voice:live:4:7:i_feel_lonely_can_we_talk] path=llm unexpected reply="
                "Absolutely, feeling lonely is a common feeling. Let's chat about anything you'd like."
            ),
            "debug_report": {
                "category": "logic_bug",
                "fixable_by_agent": False,
                "code_bug": {
                    "is_code_bug": True,
                    "bug_summary": "STT empty on bot",
                },
            },
        }
        ui = classify_incident_for_ui(raw)
        self.assertEqual(ui["issue_kind"], "agent_auto_fixed")
        self.assertEqual(ui["queue"], "agent_working")
        self.assertTrue(ui["handled_by_agent"])
        self.assertTrue(ui["auto_resolves_on_tick"])
        self.assertFalse(ui["show_developer_issue"])
        self.assertTrue(ui["fixable_by_intelligent_mode"])
        self.assertIn("chat", ui["soak_reply_text"])

    def test_wav_too_large_is_agent_handled(self) -> None:
        raw = {
            "device_id": "b0a6048addd4",
            "subsystem": "voice",
            "error": "ESP play_wav failed: WAV too large for ESP (828594 bytes; max 389120)",
            "debug_report": {},
        }
        ui = classify_incident_for_ui(raw)
        self.assertEqual(ui["issue_kind"], "agent_auto_fixed")
        self.assertEqual(ui["queue"], "agent_working")
        self.assertTrue(ui["handled_by_agent"])
        self.assertFalse(ui["show_developer_issue"])

    def test_enrich_adds_ui_block(self) -> None:
        enriched = enrich_incident_for_ui(
            {
                "incident_id": "abc",
                "device_id": "server",
                "subsystem": "llm",
                "error": "connection refused",
                "debug_report": {"category": "operational"},
            }
        )
        self.assertIn("ui", enriched)
        self.assertIn("issue_kind", enriched["ui"])


if __name__ == "__main__":
    unittest.main()
