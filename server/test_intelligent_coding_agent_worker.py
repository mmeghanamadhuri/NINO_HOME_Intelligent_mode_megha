import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from intelligent_mode.coding_agent import (
    CodeChange,
    FixProposal,
    gather_rich_context,
    validate_proposal,
)
from intelligent_mode.coding_agent_worker import CodingAgentWorker
from intelligent_mode.incidents import Incident


class CodingAgentSmartTests(unittest.TestCase):
    def _incident(self, **overrides: object) -> Incident:
        base = {
            "device_id": "b0a6048addd4",
            "display_name": "Robot",
            "subsystem": "voice",
            "severity": "warning",
            "tier": 1,
            "error": "LLM reply generation failed",
            "signature": "b0:voice:llm",
            "status": "escalated",
            "debug_report": {
                "code_bug": {
                    "is_code_bug": True,
                    "bug_summary": "LLM fails after STT",
                    "likely_cause": "Timeout",
                    "affected_files": ["server/llm_service.py"],
                },
            },
        }
        base.update(overrides)
        return Incident(**base)  # type: ignore[arg-type]

    def test_gather_rich_context_includes_files(self) -> None:
        ctx = gather_rich_context(
            self._incident(),
            {"bug_summary": "LLM timeout", "likely_cause": "ollama slow"},
            ["server/llm_service.py"],
        )
        self.assertIn("snippets", ctx)
        self.assertIn("keywords", ctx)

    def test_validate_proposal_checks_old_code(self) -> None:
        proposal = FixProposal(
            changes=[
                CodeChange(
                    file_path="server/llm_service.py",
                    old_code="THIS_STRING_DOES_NOT_EXIST_IN_FILE_XYZ",
                    new_code="fixed",
                )
            ]
        )
        ok, detail = validate_proposal(proposal)
        self.assertFalse(ok)
        self.assertIn("old_code not found", detail)


class CodingAgentWorkerTests(unittest.TestCase):
    def test_worker_dispatches_parallel_jobs(self) -> None:
        inc = Incident(
            device_id="bot1",
            display_name="Bot",
            subsystem="voice",
            severity="warning",
            tier=1,
            error="logic bug",
            signature="sig1",
            status="escalated",
            incident_id="inc1",
            debug_report={
                "category": "logic_bug",
                "fixable_by_agent": False,
                "code_bug": {"is_code_bug": True, "bug_summary": "bug"},
            },
        )

        worker = CodingAgentWorker(
            poll_seconds=30,
            parallel_workers=2,
            get_incidents=lambda: [inc],
        )
        worker._pool = __import__("concurrent.futures").futures.ThreadPoolExecutor(max_workers=2)

        with (
            patch("intelligent_mode.coding_agent_worker.CodingAgentWorker._already_handled", return_value=False),
            patch(
                "intelligent_mode.coding_agent.process_code_bug_incident_smart",
                return_value=FixProposal(proposal_id="p1", incident_id="inc1", email_sent=True),
            ),
        ):
            summary = worker.run_once()

        self.assertEqual(summary["dispatched"], 1)
        worker.stop()


if __name__ == "__main__":
    unittest.main()
