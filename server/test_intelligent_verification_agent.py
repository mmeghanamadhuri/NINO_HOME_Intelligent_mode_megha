import unittest
from unittest.mock import patch

from intelligent_mode.incidents import Incident
from intelligent_mode.smoke_tests import device_smoke_passed
from intelligent_mode.verification_agent import verify_incident_resolution


class VerificationAgentTests(unittest.TestCase):
    def _voice_incident(self) -> Incident:
        return Incident(
            device_id="b0a6048addd4",
            display_name="Robot b0a6048addd4",
            subsystem="voice",
            severity="critical",
            tier=1,
            error=(
                "Network request failed (HTTPConnectionPool(host='127.0.0.1', port=11434): "
                "Max retries exceeded with url: /api/generate"
            ),
            signature="b0a6048addd4:voice:ollama-down",
        )

    def _snapshot(self, *, llm_reachable: bool = True) -> dict:
        return {
            "devices": {
                "devices": [
                    {
                        "device_id": "b0a6048addd4",
                        "display_name": "Robot",
                        "base_url": "http://192.168.1.148",
                        "camera_url": "http://192.168.1.148/stream",
                        "play_wav_url": "http://192.168.1.148/play_wav",
                    }
                ]
            },
            "llm": {
                "reachable": llm_reachable,
                "loaded": llm_reachable,
                "model": "qwen2.5:1.5b",
                "base_url": "http://127.0.0.1:11435",
                "warning": "" if llm_reachable else "connection refused",
            },
            "stt": {"provider": "whisper", "loaded": True},
            "tts": {"last_error": ""},
            "discovery": {"last_error": ""},
            "cameras": {"b0a6048addd4": {"connected": True, "last_frame_age_seconds": 1.0}},
        }

    @patch("intelligent_mode.verification_agent._probe_ollama_generate", return_value=(False, "connection refused"))
    @patch("intelligent_mode.smoke_tests.device_smoke_passed", return_value=True)
    @patch("network_util.voice_ws_url_for_esp", return_value="ws://127.0.0.1:8000/voice-query")
    def test_ollama_down_blocks_resolve(
        self,
        _ws: object,
        _smoke: object,
        _probe: object,
    ) -> None:
        result = verify_incident_resolution(
            self._voice_incident(),
            self._snapshot(llm_reachable=False),
            live_probes=True,
        )
        self.assertFalse(result.passed)
        names = {check.name for check in result.checks if not check.passed}
        self.assertIn("ollama_live_generate", names)

    @patch("intelligent_mode.verification_agent._probe_ollama_generate", return_value=(True, "ok"))
    @patch("intelligent_mode.smoke_tests.run_smoke_for_device")
    @patch("intelligent_mode.smoke_tests.run_smoke_suite")
    @patch("network_util.voice_ws_url_for_esp", return_value="ws://127.0.0.1:8000/voice-query")
    def test_verified_when_all_checks_pass(
        self,
        _ws: object,
        mock_suite: object,
        mock_device: object,
        _probe: object,
    ) -> None:
        from intelligent_mode.smoke_tests import SmokeTestResult, SmokeTestRun

        ok = SmokeTestRun()
        ok.results = [
            SmokeTestResult(
                test_id="server:ollama_reachable",
                name="server:ollama_reachable",
                device_id="server",
                subsystem="llm",
                passed=True,
                message="ok",
            )
        ]
        ok.total = 1
        ok.passed = 1
        mock_suite.return_value = ok
        mock_device.return_value = ok

        result = verify_incident_resolution(
            self._voice_incident(),
            self._snapshot(llm_reachable=True),
            live_probes=True,
            mode="post_fix",
        )
        self.assertTrue(result.passed)

    @patch("intelligent_mode.smoke_tests.probe_bot_snapshot", return_value=(True, "", 200))
    @patch("intelligent_mode.smoke_tests._http_get_ok", return_value=(True, ""))
    @patch("network_util.voice_ws_url_for_esp", return_value="ws://127.0.0.1:8000/voice-query")
    def test_device_smoke_requires_server_for_voice_bot(self, _ws: object, _http: object, _probe: object) -> None:
        from intelligent_mode.smoke_tests import SmokeTestResult, run_smoke_suite

        snap = self._snapshot(llm_reachable=False)
        with patch.object(
            __import__("intelligent_mode.smoke_tests", fromlist=["run_smoke_suite"]),
            "run_smoke_suite",
            wraps=run_smoke_suite,
        ):
            passed = device_smoke_passed(snap, "b0a6048addd4", subsystem="voice")
        self.assertFalse(passed)


if __name__ == "__main__":
    unittest.main()
