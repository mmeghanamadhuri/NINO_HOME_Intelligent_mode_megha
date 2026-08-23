import unittest

from intelligent_mode.agent_remediation import (
    classify_agent_remediation,
    is_agent_remediatable_incident,
)
from intelligent_mode.code_bug_analyzer import analyze_code_bug, is_code_bug_incident
from intelligent_mode.incidents import Incident


class AgentRemediationTests(unittest.TestCase):
    def _incident(self, **overrides: object) -> Incident:
        base = {
            "device_id": "b0a6048addd4",
            "display_name": "Robot",
            "subsystem": "voice",
            "severity": "warning",
            "tier": 1,
            "error": "",
            "signature": "sig",
            "status": "open",
        }
        base.update(overrides)
        return Incident(**base)  # type: ignore[arg-type]

    def test_wav_too_large_is_agent_handled(self) -> None:
        inc = self._incident(
            error="ESP play_wav failed: WAV too large for ESP (800370 bytes; max 389120)"
        )
        plan = classify_agent_remediation(inc)
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.pattern_id, "wav_auto_split")
        self.assertTrue(is_agent_remediatable_incident(inc))
        self.assertFalse(is_code_bug_incident(inc))
        self.assertFalse(analyze_code_bug(inc, use_llm=False).is_code_bug)

    def test_stt_empty_is_agent_handled(self) -> None:
        inc = self._incident(error="STT path=stt_empty")
        plan = classify_agent_remediation(inc)
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.pattern_id, "voice_stt_recovery")
        self.assertIn("voice_pipeline_recovery", plan.recovery_actions)
        self.assertFalse(is_code_bug_incident(inc))

    def test_unexpected_llm_reply_is_agent_handled(self) -> None:
        inc = self._incident(
            error=(
                "[soak:soak:voice:live:4:7:i_feel_lonely_can_we_talk] path=llm unexpected reply="
                "Absolutely, feeling lonely is common. Let's chat about anything you'd like."
            )
        )
        plan = classify_agent_remediation(inc)
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.pattern_id, "soak_valid_reply")
        self.assertFalse(is_code_bug_incident(inc))

    def test_unexpected_reply_retries_pipeline(self) -> None:
        inc = self._incident(
            error="[soak:voice] path=alarm unexpected reply=OK"
        )
        plan = classify_agent_remediation(inc)
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.pattern_id, "soak_reply_recovery")


if __name__ == "__main__":
    unittest.main()
