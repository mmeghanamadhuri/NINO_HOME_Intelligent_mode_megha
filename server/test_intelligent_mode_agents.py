"""Additional tests for hardened intelligent mode behavior."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from intelligent_mode.config import IntelligentConfig
from intelligent_mode.context import IntelligentContext, configure_context
from intelligent_mode.detectors import GraceTracker, detect_anomalies
from intelligent_mode.digest import EmailDigestQueue
from intelligent_mode.incidents import Incident
from intelligent_mode.orchestrator import IntelligentOrchestrator
from intelligent_mode.workers import (
    ALLOWED_FIX_ACTIONS,
    CameraWorker,
    apply_fix,
    verify_incident_with_smoke,
)


class CameraGraceTests(unittest.TestCase):
    def test_camera_can_use_shorter_grace_override(self) -> None:
        grace = GraceTracker(grace_seconds=999)
        snap = {
            "devices": {
                "devices": [
                    {
                        "device_id": "aa:bb:cc:dd:ee:ff",
                        "display_name": "Kitchen",
                        "base_url": "http://192.168.0.10",
                    }
                ]
            },
            "cameras": {"aa:bb:cc:dd:ee:ff": {"connected": False, "last_error": ""}},
            "bot_runtime": {
                "aa:bb:cc:dd:ee:ff": {
                    "session_active": True,
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
        with patch("intelligent_mode.detectors.probe_bot_snapshot", return_value=(True, "", 200)):
            none_yet = detect_anomalies(snap, grace=grace, camera_grace_seconds=999)
            immediate = detect_anomalies(snap, grace=GraceTracker(0), camera_grace_seconds=0)
        self.assertFalse(any(f.subsystem == "camera" for f in none_yet))
        self.assertTrue(any(f.subsystem == "camera" for f in immediate))


class DigestTests(unittest.TestCase):
    def test_queues_non_critical_for_digest(self) -> None:
        cfg = IntelligentConfig(enabled=True, email_mode="digest", email_to="a@b.com", smtp_host="smtp.test")
        digest = EmailDigestQueue(cfg)
        inc = Incident(
            device_id="server",
            display_name="Server",
            subsystem="tts",
            severity="warning",
            tier=1,
            error="warn",
            signature="s",
            status="resolved",
        )
        self.assertTrue(digest.enqueue(inc))
        self.assertEqual(digest.pending_count(), 1)

    def test_critical_not_queued(self) -> None:
        cfg = IntelligentConfig(enabled=True, email_mode="digest", email_to="a@b.com", smtp_host="smtp.test")
        digest = EmailDigestQueue(cfg)
        inc = Incident(
            device_id="server",
            display_name="Server",
            subsystem="llm",
            severity="critical",
            tier=0,
            error="down",
            signature="s",
            status="open",
        )
        self.assertFalse(digest.enqueue(inc))


class TierAndWorkerTests(unittest.TestCase):
    def test_tier_two_escalates_without_fix(self) -> None:
        snapshot = {
            "devices": {"devices": []},
            "cameras": {},
            "llm": {"reachable": True},
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
            IntelligentConfig(enabled=True, grace_seconds=0, max_auto_fix_tier=1)
        )
        inc = Incident(
            device_id="server",
            display_name="Server",
            subsystem="memory",
            severity="warning",
            tier=2,
            error="db",
            signature="server:memory:db",
            status="open",
        )
        orch._maybe_fix(inc, snapshot)
        self.assertEqual(inc.status, "escalated")
        self.assertEqual(inc.fix_attempts, 0)

    def test_camera_worker_scoped_to_device(self) -> None:
        registry = MagicMock()
        record = MagicMock()
        record.effective_camera_url.return_value = "http://192.168.0.10/stream"
        registry.get.return_value = record
        cameras = MagicMock()
        cameras.status.return_value = {"connected": True}
        ctx = IntelligentContext(
            registry=registry,
            cameras=cameras,
            tts=MagicMock(),
            face_registration=MagicMock(),
            collect_status=lambda: {
                "bot_runtime": {
                    "aa:bb:cc:dd:ee:ff": {
                        "session_active": True,
                        "streaming": False,
                        "uvc_connected": True,
                    }
                },
                "cameras": {"aa:bb:cc:dd:ee:ff": {"connected": False}},
            },
        )
        inc = Incident(
            device_id="aa:bb:cc:dd:ee:ff",
            display_name="Kitchen",
            subsystem="camera",
            severity="critical",
            tier=0,
            error="down",
            signature="x",
        )
        result = CameraWorker().try_fix(inc, ctx)
        cameras.restart.assert_called_once_with("aa:bb:cc:dd:ee:ff", "http://192.168.0.10/stream")
        self.assertEqual(result.action, "camera_restart")
        self.assertIn(result.action, ALLOWED_FIX_ACTIONS)


class SmokeVerifyTests(unittest.TestCase):
    def test_verify_with_smoke_requires_bot_tests(self) -> None:
        inc = Incident(
            device_id="aa:bb:cc:dd:ee:ff",
            display_name="Kitchen",
            subsystem="camera",
            severity="critical",
            tier=0,
            error="down",
            signature="x",
        )
        snap = {
            "devices": {
                "devices": [
                    {
                        "device_id": "aa:bb:cc:dd:ee:ff",
                        "base_url": "http://192.168.0.10",
                        "camera_url": "http://192.168.0.10/stream",
                        "play_wav_url": "http://192.168.0.10/play_wav",
                    }
                ]
            },
            "cameras": {"aa:bb:cc:dd:ee:ff": {"connected": True}},
            "llm": {"reachable": True},
            "memory": {"database_url_set": False},
            "stt": {"provider": "elevenlabs", "loaded": True},
            "tts": {"last_error": ""},
            "discovery": {"last_error": ""},
        }
        with patch("intelligent_mode.smoke_tests.probe_bot_snapshot", return_value=(True, "", 200)):
            with patch("intelligent_mode.smoke_tests._http_get_ok", return_value=(True, "")):
                self.assertTrue(verify_incident_with_smoke(inc, snap))


class CodeBugResolutionTests(unittest.TestCase):
    def test_wav_too_large_runs_agent_fix(self) -> None:
        snapshot = {
            "devices": {"devices": []},
            "cameras": {},
            "llm": {"reachable": True},
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
                autonomous_recovery_enabled=True,
            )
        )
        inc = Incident(
            device_id="b0a6048addd4",
            display_name="Robot",
            subsystem="voice",
            severity="critical",
            tier=0,
            error="[soak:voice] ESP play_wav failed: WAV too large for ESP (463798 bytes; max 389120)",
            signature="b0:voice:wav",
            status="open",
        )
        with patch(
            "intelligent_mode.recovery._voice_pipeline_recovery",
            return_value=(True, "pipeline ok"),
        ):
            orch._maybe_fix(inc, snapshot)
        self.assertGreater(inc.fix_attempts, 0)
        self.assertTrue(any(f.action == "voice_pipeline_recovery" for f in inc.fixes))

    def test_true_code_bug_skips_auto_fix(self) -> None:
        snapshot = {
            "devices": {"devices": []},
            "cameras": {},
            "llm": {"reachable": True},
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
                autonomous_recovery_enabled=True,
            )
        )
        inc = Incident(
            device_id="b0a6048addd4",
            display_name="Robot",
            subsystem="camera",
            severity="critical",
            tier=0,
            error="Camera regression in firmware session gate",
            signature="b0:camera:regression",
            status="open",
            debug_report={
                "category": "logic_bug",
                "fixable_by_agent": False,
                "code_bug": {
                    "is_code_bug": True,
                    "bug_summary": "Camera firmware regression",
                },
            },
        )
        orch._maybe_fix(inc, snapshot)
        self.assertEqual(inc.status, "escalated")
        self.assertEqual(inc.fix_attempts, 0)
        self.assertEqual(inc.fixes, [])

    def test_code_bug_never_marked_resolved_on_verify(self) -> None:
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
            voice_active_fn=lambda _d: False,
        )
        configure_context(ctx)
        orch = IntelligentOrchestrator(IntelligentConfig(enabled=True, grace_seconds=0))
        inc = Incident(
            device_id="b0a6048addd4",
            display_name="Robot",
            subsystem="camera",
            severity="critical",
            tier=0,
            error="Camera regression in firmware session gate",
            signature="b0:camera:regression",
            status="fixing",
            debug_report={
                "category": "logic_bug",
                "fixable_by_agent": False,
                "code_bug": {"is_code_bug": True, "bug_summary": "Camera firmware regression"},
            },
        )
        with patch(
            "intelligent_mode.orchestrator.verify_incident_with_smoke",
            return_value=True,
        ):
            resolved = orch._verify_and_finalize(inc)
        self.assertFalse(resolved)
        self.assertEqual(inc.status, "escalated")
        self.assertIsNone(inc.resolved_at)


if __name__ == "__main__":
    unittest.main()
