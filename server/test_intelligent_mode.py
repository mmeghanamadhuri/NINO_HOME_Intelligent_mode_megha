"""Tests for NiNO intelligent mode detectors and incident store."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from intelligent_mode.detectors import GraceTracker, detect_anomalies
from intelligent_mode.incidents import Incident, IncidentStore
from intelligent_mode.workers import verify_incident_cleared


class GraceTrackerTests(unittest.TestCase):
    def test_requires_persistence(self) -> None:
        grace = GraceTracker(grace_seconds=60)
        self.assertFalse(grace.ready("k", True))
        self.assertFalse(grace.ready("k", True))
        grace._grace_seconds = 0
        self.assertTrue(grace.ready("k", True))
        grace.reset("k")
        self.assertFalse(grace.ready("k", False))


class DetectorTests(unittest.TestCase):
    def test_detects_llm_down(self) -> None:
        grace = GraceTracker(grace_seconds=0)
        snapshot = {
            "devices": {"devices": []},
            "cameras": {},
            "llm": {"reachable": False, "warning": "connection refused"},
            "memory": {"database_url_set": False, "ready": False},
            "stt": {"provider": "elevenlabs", "loaded": True},
            "tts": {"last_error": ""},
            "discovery": {"last_error": ""},
        }
        found = detect_anomalies(snapshot, grace=grace)
        subs = {item.subsystem for item in found}
        self.assertIn("llm", subs)

    def test_detects_camera_down_for_bot(self) -> None:
        grace = GraceTracker(grace_seconds=0)
        snapshot = {
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
                "aa:bb:cc:dd:ee:ff": {
                    "connected": False,
                    "last_error": "timeout",
                }
            },
            "bot_runtime": {
                "aa:bb:cc:dd:ee:ff": {
                    "session_active": True,
                    "streaming": False,
                    "uvc_connected": True,
                }
            },
            "llm": {"reachable": True},
            "memory": {"database_url_set": False},
            "stt": {"provider": "whisper", "loaded": True},
            "tts": {"last_error": ""},
            "discovery": {"last_error": ""},
        }
        with patch("intelligent_mode.detectors.probe_bot_snapshot", return_value=(True, "", 200)):
            found = detect_anomalies(snapshot, grace=grace)
        cam = next(i for i in found if i.subsystem == "camera")
        self.assertEqual(cam.device_id, "aa:bb:cc:dd:ee:ff")


class IncidentStoreTests(unittest.TestCase):
    def test_dedupes_open_incidents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "incidents.json"
            store = IncidentStore(path=path)
            inc = Incident(
                device_id="aa:bb:cc:dd:ee:ff",
                display_name="Kitchen",
                subsystem="camera",
                severity="warning",
                tier=0,
                error="timeout",
                signature=Incident.make_signature("aa:bb:cc:dd:ee:ff", "camera", "timeout"),
            )
            first = store.open_incident(inc)
            second = store.open_incident(inc)
            self.assertEqual(first[0].incident_id, second[0].incident_id)
            self.assertFalse(second[1])
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(data["incidents"]), 1)


class VerifyTests(unittest.TestCase):
    def test_verify_llm_cleared(self) -> None:
        inc = Incident(
            device_id="server",
            display_name="Server",
            subsystem="llm",
            severity="critical",
            tier=0,
            error="down",
            signature="server:llm:down",
        )
        self.assertTrue(
            verify_incident_cleared(inc, {"llm": {"reachable": True}})
        )
        self.assertFalse(
            verify_incident_cleared(inc, {"llm": {"reachable": False}})
        )


if __name__ == "__main__":
    unittest.main()
