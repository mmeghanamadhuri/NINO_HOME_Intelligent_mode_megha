"""Smoke test suite unit tests."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from intelligent_mode.smoke_tests import failures_to_candidates, run_smoke_suite


class SmokeSuiteTests(unittest.TestCase):
    def _healthy_snapshot(self) -> dict:
        return {
            "devices": {
                "devices": [
                    {
                        "device_id": "aa:bb:cc:dd:ee:ff",
                        "display_name": "Kitchen",
                        "base_url": "http://192.168.0.10",
                        "camera_url": "http://192.168.0.10/stream",
                        "play_wav_url": "http://192.168.0.10/play_wav",
                    }
                ]
            },
            "cameras": {
                "aa:bb:cc:dd:ee:ff": {
                    "connected": True,
                    "last_frame_age_seconds": 0.5,
                    "last_error": "",
                }
            },
            "llm": {"reachable": True, "loaded": True, "model": "qwen2.5:1.5b", "base_url": "http://127.0.0.1:11435"},
            "memory": {"database_url_set": False, "ready": False},
            "stt": {"provider": "whisper", "loaded": True, "model": "small", "device": "cuda"},
            "tts": {"last_error": ""},
            "discovery": {"last_error": ""},
        }

    @patch("intelligent_mode.smoke_tests.probe_bot_snapshot", return_value=(True, "", 200))
    @patch("intelligent_mode.smoke_tests._http_get_ok", return_value=(True, ""))
    @patch("network_util.voice_ws_url_for_esp", return_value="ws://192.168.0.100:8000/voice-query")
    def test_all_pass_when_healthy(
        self,
        _ws: object,
        _http: object,
        _probe: object,
    ) -> None:
        run = run_smoke_suite(self._healthy_snapshot())
        self.assertEqual(run.failed, 0)
        self.assertGreater(run.total, 0)
        self.assertTrue(run.to_dict()["ok"])

    @patch("intelligent_mode.smoke_tests.probe_bot_snapshot", return_value=(False, "timeout", None))
    @patch("intelligent_mode.smoke_tests._http_get_ok", return_value=(False, "timeout"))
    @patch("network_util.voice_ws_url_for_esp", return_value="ws://192.168.0.100:8000/voice-query")
    def test_bot_failures_create_candidates(
        self,
        _ws: object,
        _http: object,
        _probe: object,
    ) -> None:
        snap = self._healthy_snapshot()
        snap["cameras"]["aa:bb:cc:dd:ee:ff"]["connected"] = False
        snap["bot_runtime"] = {
            "aa:bb:cc:dd:ee:ff": {
                "session_active": True,
                "streaming": False,
                "uvc_connected": True,
            }
        }
        run = run_smoke_suite(snap)
        self.assertGreater(run.failed, 0)
        candidates = failures_to_candidates(run)
        subs = {c.subsystem for c in candidates}
        self.assertIn("camera", subs)
        self.assertTrue(any("[smoke:" in c.error for c in candidates))

    def test_llm_down_fails_smoke(self) -> None:
        snap = self._healthy_snapshot()
        snap["llm"] = {"reachable": False, "loaded": False, "warning": "connection refused"}
        with patch("network_util.voice_ws_url_for_esp", return_value="ws://x/y"):
            with patch("intelligent_mode.smoke_tests.probe_bot_snapshot", return_value=(True, "", 200)):
                with patch("intelligent_mode.smoke_tests._http_get_ok", return_value=(True, "")):
                    run = run_smoke_suite(snap)
        failed = [r for r in run.results if not r.passed and r.subsystem == "llm"]
        self.assertTrue(failed)


class SmokeOrchestratorIntegrationTests(unittest.TestCase):
    def test_orchestrator_runs_smoke_when_enabled(self) -> None:
        from intelligent_mode.config import IntelligentConfig
        from intelligent_mode.context import IntelligentContext, configure_context
        from intelligent_mode.orchestrator import IntelligentOrchestrator

        snapshot = {
            "devices": {"devices": []},
            "cameras": {},
            "llm": {"reachable": True, "loaded": True},
            "memory": {"database_url_set": False},
            "stt": {"provider": "elevenlabs", "loaded": True},
            "tts": {"last_error": ""},
            "discovery": {"last_error": ""},
        }
        ctx = IntelligentContext(
            registry=MagicMock(),
            cameras=MagicMock(),
            tts=MagicMock(),
            face_registration=MagicMock(),
            collect_status=lambda: snapshot,
        )
        configure_context(ctx)
        orch = IntelligentOrchestrator(
            IntelligentConfig(
                enabled=True,
                grace_seconds=0,
                smoke_tests_enabled=True,
                e2e_tests_enabled=False,
            )
        )
        with patch("network_util.voice_ws_url_for_esp", return_value="ws://host/voice-query"):
            summary = orch.run_once()
        self.assertIn("smoke_tests", summary)
        self.assertIsNotNone(summary["smoke_tests"])


if __name__ == "__main__":
    unittest.main()
