import unittest

from intelligent_mode.dashboard import build_dashboard


class DashboardTests(unittest.TestCase):
    def test_build_dashboard_summary(self) -> None:
        snapshot = {
            "devices": {
                "devices": [
                    {
                        "device_id": "aa11bb22cc33",
                        "display_name": "Living Room",
                        "base_url": "http://192.168.1.10",
                        "camera_url": "http://192.168.1.10/stream",
                        "play_wav_url": "http://192.168.1.10/play_wav",
                    }
                ]
            },
            "cameras": {
                "aa11bb22cc33": {"connected": True, "last_frame_age_seconds": 1.2},
            },
            "llm": {"reachable": True, "loaded": True, "model": "qwen2.5:1.5b"},
            "memory": {"database_url_set": False},
            "stt": {"provider": "elevenlabs"},
            "tts": {"last_error": ""},
        }
        incidents = [
            {
                "incident_id": "abc123",
                "device_id": "aa11bb22cc33",
                "display_name": "Living Room",
                "subsystem": "camera",
                "severity": "critical",
                "tier": 0,
                "error": "Camera stream disconnected",
                "signature": "aa11bb22cc33:camera:down",
                "status": "fixing",
                "detected_at": "2026-08-23T00:00:00+00:00",
                "updated_at": "2026-08-23T00:00:00+00:00",
                "fix_attempts": 1,
                "fixes": [
                    {
                        "action": "camera_restart",
                        "success": False,
                        "detail": "still down",
                        "at": "2026-08-23T00:00:10+00:00",
                    }
                ],
            }
        ]
        last_smoke_run = {
            "run_id": "run1",
            "started_at": "2026-08-23T00:00:00+00:00",
            "finished_at": "2026-08-23T00:00:01+00:00",
            "passed": 4,
            "failed": 1,
            "total": 5,
            "results": [
                {
                    "test_id": "server:ollama_reachable",
                    "name": "server:ollama_reachable",
                    "device_id": "server",
                    "subsystem": "llm",
                    "passed": True,
                    "message": "ok",
                },
                {
                    "test_id": "bot:aa11bb22cc33:camera_live",
                    "name": "bot:aa11bb22cc33:camera_live",
                    "device_id": "aa11bb22cc33",
                    "subsystem": "camera",
                    "passed": False,
                    "message": "disconnected",
                },
            ],
        }

        payload = build_dashboard(
            snapshot=snapshot,
            intelligent_status={"enabled": True, "running": True, "last_tick": {"opened": 1}},
            incidents=incidents,
            last_smoke_run=last_smoke_run,
            voice_active_fn=lambda device_id=None: device_id == "aa11bb22cc33",
        )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["summary"]["total_bots"], 1)
        self.assertEqual(payload["summary"]["needs_help_bots"], 1)
        self.assertEqual(payload["summary"]["open_incidents"], 1)
        self.assertIn("issue_queues", payload)
        self.assertIn("agent_working", payload["issue_queues"])
        self.assertIn("developer", payload["issue_queues"])
        self.assertIn("agent_resolved", payload["issue_queues"])
        self.assertEqual(payload["summary"]["agent_handling_incidents"], 1)
        self.assertEqual(len(payload["issue_queues"]["agent_working"]), 1)
        self.assertEqual(payload["bots"][0]["agent_status"], "fixing")
        self.assertTrue(payload["bots"][0]["voice_pipeline_active"])
        self.assertEqual(payload["server"]["health"], "healthy")


if __name__ == "__main__":
    unittest.main()
