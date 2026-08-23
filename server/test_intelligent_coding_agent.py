import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from intelligent_mode.coding_agent import (
    CodeChange,
    FixProposal,
    _parse_llm_json,
    approve_proposal,
    build_proposal_email,
    get_proposal,
    list_available_coding_models,
    list_proposals,
    propose_fix,
    reject_proposal,
    select_coding_model,
)
from intelligent_mode.incidents import Incident


class CodingAgentParseTests(unittest.TestCase):
    def test_parse_json_with_fences(self) -> None:
        raw = '```json\n{"bug_summary": "test", "fix_type": "server"}\n```'
        parsed = _parse_llm_json(raw)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["bug_summary"], "test")

    def test_parse_embedded_json(self) -> None:
        raw = 'Here is the fix:\n{"bug_summary": "camera bug", "changes": []}\nDone.'
        parsed = _parse_llm_json(raw)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["bug_summary"], "camera bug")


class CodingAgentProposalTests(unittest.TestCase):
    def _incident(self, **overrides: object) -> Incident:
        base = {
            "device_id": "b0a6048addd4",
            "display_name": "Robot",
            "subsystem": "voice",
            "severity": "warning",
            "tier": 1,
            "error": "LLM reply generation failed after STT",
            "signature": "b0:voice:llm",
            "status": "escalated",
            "debug_report": {
                "category": "logic_bug",
                "fixable_by_agent": False,
                "code_bug": {
                    "is_code_bug": True,
                    "bug_summary": "Voice reaches STT but LLM fails",
                    "likely_cause": "Ollama timeout in llm_service",
                    "suggested_fix": "Increase timeout in llm_service.py",
                    "affected_files": ["server/llm_service.py"],
                    "server_change_recommended": True,
                    "firmware_update_recommended": False,
                },
            },
        }
        base.update(overrides)
        return Incident(**base)  # type: ignore[arg-type]

    def test_propose_fix_creates_proposal(self) -> None:
        llm_json = json.dumps(
            {
                "bug_summary": "LLM timeout too short",
                "root_cause": "VOICE_QUERY_TIMEOUT_S is 60s",
                "fix_type": "server",
                "changes": [
                    {
                        "file_path": "server/llm_service.py",
                        "start_line": 35,
                        "end_line": 35,
                        "old_code": "VOICE_QUERY_TIMEOUT_S = 60",
                        "new_code": "VOICE_QUERY_TIMEOUT_S = 90",
                        "explanation": "Increase timeout for slow GPU cold start",
                    }
                ],
                "manual_steps": [],
                "confidence": "high",
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            proposals_path = Path(tmp) / "proposals.json"
            with (
                patch.dict("os.environ", {"CODING_AGENT_ENABLED": "1"}),
                patch("intelligent_mode.coding_agent._PROPOSALS_PATH", proposals_path),
                patch("intelligent_mode.coding_agent.ollama_is_reachable", create=True),
                patch(
                    "llm_service.ollama_is_reachable",
                    return_value=True,
                ),
                patch(
                    "llm_service.ollama_generate",
                    return_value=llm_json,
                ),
                patch(
                    "llm_service.resolve_ollama_api_url",
                    return_value="http://127.0.0.1:11435/api/generate",
                ),
                patch(
                    "intelligent_mode.coding_agent.select_coding_model",
                    return_value=("qwen2.5-coder:32b", "test model"),
                ),
            ):
                proposal = propose_fix(self._incident(), force=True)
        self.assertIsNotNone(proposal)
        assert proposal is not None
        self.assertEqual(proposal.fix_type, "server")
        self.assertEqual(len(proposal.changes), 1)
        self.assertEqual(proposal.changes[0].file_path, "server/llm_service.py")
        self.assertEqual(proposal.model_used, "qwen2.5-coder:32b")

    def test_email_contains_approve_links(self) -> None:
        proposal = FixProposal(
            proposal_id="abc123",
            incident_id="inc1",
            device_id="b0a6048addd4",
            display_name="Robot",
            subsystem="voice",
            bug_summary="LLM timeout",
            root_cause="Timeout too short",
            error="LLM failed",
            changes=[
                CodeChange(
                    file_path="server/llm_service.py",
                    old_code="VOICE_QUERY_TIMEOUT_S = 60",
                    new_code="VOICE_QUERY_TIMEOUT_S = 90",
                    explanation="Increase timeout",
                )
            ],
            fix_type="server",
            model_used="qwen2.5-coder:32b",
        )
        with patch.dict("os.environ", {"CODING_AGENT_APPROVAL_TOKEN": "secret"}):
            subject, body = build_proposal_email(proposal)
        self.assertIn("LLM timeout", subject)
        self.assertIn("abc123", body)
        self.assertIn("/api/coding-agent/approve/abc123", body)
        self.assertIn("/api/coding-agent/reject/abc123", body)
        self.assertIn("--- BEFORE ---", body)
        self.assertIn("--- AFTER ---", body)
        self.assertIn("APPROVE OR REJECT", body)

    def test_approve_reject_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proposals_path = Path(tmp) / "proposals.json"
            proposal = FixProposal(proposal_id="test01", status="pending")
            proposals_path.write_text(json.dumps([proposal.to_dict()]), encoding="utf-8")
            with patch("intelligent_mode.coding_agent._PROPOSALS_PATH", proposals_path):
                result = reject_proposal("test01")
                self.assertTrue(result["ok"])
                updated = get_proposal("test01")
                assert updated is not None
                self.assertEqual(updated.status, "rejected")

    def test_disabled_when_env_off(self) -> None:
        with patch.dict("os.environ", {"CODING_AGENT_ENABLED": "0"}):
            result = propose_fix(self._incident())
        self.assertIsNone(result)


class CodingAgentModelTests(unittest.TestCase):
    def test_uses_qwen32b_only(self) -> None:
        with (
            patch.dict("os.environ", {"CODING_AGENT_MODEL": "qwen2.5-coder:7b"}),
            patch("llm_service.ollama_model_available", return_value=True),
            patch("llm_service.resolve_ollama_api_url", return_value="http://127.0.0.1:11435/api/generate"),
        ):
            model, reason = select_coding_model()
        self.assertEqual(model, "qwen2.5-coder:32b")
        self.assertIn("qwen2.5-coder:32b", reason)

    def test_list_models_shows_required_32b(self) -> None:
        with (
            patch("llm_service.ollama_is_reachable", return_value=True),
            patch("llm_service.ollama_model_available", return_value=False),
            patch("llm_service.resolve_ollama_api_url", return_value="http://127.0.0.1:11435/api/generate"),
        ):
            models = list_available_coding_models()
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0]["model"], "qwen2.5-coder:32b")
        self.assertTrue(models[0]["required"])


if __name__ == "__main__":
    unittest.main()
