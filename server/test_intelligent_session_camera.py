"""Session-gated camera architecture tests."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from intelligent_mode.detectors import GraceTracker, detect_anomalies
from intelligent_mode.session_camera import (
    camera_expects_stream,
    classify_camera_state,
    snapshot_acceptable,
)
from intelligent_mode.smoke_tests import run_smoke_suite
from intelligent_mode.workers import verify_incident_cleared
from intelligent_mode.incidents import Incident


class SessionCameraHelperTests(unittest.TestCase):
    def test_idle_503_is_acceptable(self) -> None:
        self.assertTrue(snapshot_acceptable(False, "HTTP 503", 503, expects_stream=False))
        self.assertFalse(snapshot_acceptable(False, "HTTP 503", 503, expects_stream=True))

    def test_classify_idle_when_no_session(self) -> None:
        snap = {
            "bot_runtime": {
                "aa11": {
                    "session_active": False,
                    "streaming": False,
                    "uvc_connected": True,
                }
            }
        }
        state = classify_camera_state(snap, "aa11", {"connected": False})
        self.assertEqual(state, "idle")

    def test_classify_fault_during_session(self) -> None:
        snap = {
            "bot_runtime": {
                "aa11": {
                    "session_active": True,
                    "streaming": False,
                    "uvc_connected": True,
                }
            }
        }
        state = classify_camera_state(snap, "aa11", {"connected": False, "last_error": "timeout"})
        self.assertEqual(state, "fault")


class SessionAwareDetectorTests(unittest.TestCase):
    def _base_snapshot(self) -> dict:
        return {
            "devices": {
                "devices": [
                    {
                        "device_id": "aa:bb:cc:dd:ee:ff",
                        "display_name": "Kitchen",
                        "base_url": "http://192.168.0.10",
                    }
                ]
            },
            "cameras": {
                "aa:bb:cc:dd:ee:ff": {"connected": False, "last_error": ""},
            },
            "bot_runtime": {
                "aa:bb:cc:dd:ee:ff": {
                    "session_active": False,
                    "streaming": False,
                    "uvc_connected": True,
                }
            },
            "llm": {"reachable": True},
            "memory": {"database_url_set": False},
            "stt": {"provider": "elevenlabs", "loaded": True},
            "tts": {"last_error": ""},
            "discovery": {"last_error": ""},
        }

    @patch(
        "intelligent_mode.detectors.probe_bot_snapshot",
        return_value=(False, "HTTP 503", 503),
    )
    def test_idle_camera_not_flagged(self, _probe: object) -> None:
        grace = GraceTracker(grace_seconds=0)
        found = detect_anomalies(self._base_snapshot(), grace=grace)
        subs = {item.subsystem for item in found}
        self.assertNotIn("camera", subs)
        self.assertNotIn("bot", subs)

    @patch(
        "intelligent_mode.detectors.probe_bot_snapshot",
        return_value=(False, "HTTP 503", 503),
    )
    def test_session_active_camera_flagged(self, _probe: object) -> None:
        snap = self._base_snapshot()
        snap["bot_runtime"]["aa:bb:cc:dd:ee:ff"]["session_active"] = True
        grace = GraceTracker(grace_seconds=0)
        found = detect_anomalies(snap, grace=grace)
        subs = {item.subsystem for item in found}
        self.assertIn("camera", subs)


class SessionAwareSmokeTests(unittest.TestCase):
    def test_idle_bot_passes_camera_smoke(self) -> None:
        snap = {
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
            "cameras": {"aa11bb22cc33": {"connected": False, "last_error": ""}},
            "bot_runtime": {
                "aa11bb22cc33": {
                    "session_active": False,
                    "streaming": False,
                    "uvc_connected": True,
                }
            },
            "llm": {"reachable": True, "loaded": True, "model": "qwen2.5:1.5b", "base_url": "http://127.0.0.1:11435"},
            "memory": {"database_url_set": False, "ready": False},
            "stt": {"provider": "whisper", "loaded": True, "model": "small", "device": "cuda"},
            "tts": {"last_error": ""},
            "discovery": {"last_error": ""},
        }
        with patch(
            "intelligent_mode.smoke_tests.probe_bot_snapshot",
            return_value=(False, "HTTP 503", 503),
        ):
            with patch("intelligent_mode.smoke_tests._http_get_ok", return_value=(True, "")):
                with patch("network_util.voice_ws_url_for_esp", return_value="ws://host/voice-query"):
                    run = run_smoke_suite(snap)
        camera = next(r for r in run.results if r.test_id.endswith(":camera_live"))
        snapshot = next(r for r in run.results if r.test_id.endswith(":snapshot_http"))
        self.assertTrue(camera.passed)
        self.assertTrue(snapshot.passed)


class SessionAwareVerifyTests(unittest.TestCase):
    def test_verify_camera_cleared_when_idle(self) -> None:
        inc = Incident(
            device_id="aa11",
            display_name="Bot",
            subsystem="camera",
            severity="critical",
            tier=0,
            error="down",
            signature="aa11:camera:down",
        )
        snap = {
            "cameras": {"aa11": {"connected": False}},
            "bot_runtime": {"aa11": {"session_active": False, "streaming": False}},
        }
        self.assertTrue(verify_incident_cleared(inc, snap))


if __name__ == "__main__":
    unittest.main()
