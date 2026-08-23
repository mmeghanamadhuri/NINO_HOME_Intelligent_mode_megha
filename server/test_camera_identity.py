"""Tests for live camera identity (no stale cache / hunt-memory greet)."""

from __future__ import annotations

import time
import unittest
from unittest.mock import patch

import app as app_module


class CameraIdentitySnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        app_module._latest_results_by_device.clear()
        app_module._latest_results_at_by_device.clear()

    def test_require_live_face_ignores_stale_cache_when_camera_off(self) -> None:
        device = "b0a6048ad4e0"
        app_module._store_face_cache(
            device,
            [{"name": "Kartik", "primary": True, "stabilized": True, "recognized": True}],
        )
        # Make cache stale.
        app_module._latest_results_at_by_device[
            app_module._results_device_key(device)
        ] = time.time() - 60.0

        with patch.object(app_module, "resolve_device_id", return_value=device):
            with patch.object(app_module.cameras, "read", return_value=None):
                name, state = app_module._camera_identity_snapshot(
                    require_live_face=True, device_id=device
                )

        self.assertIsNone(name)
        self.assertEqual(state, "no_face")

    def test_require_live_face_uses_live_frame_not_stale_cache(self) -> None:
        device = "b0a6048ad4e0"
        app_module._store_face_cache(
            device,
            [{"name": "Kartik", "primary": True, "stabilized": True, "recognized": True}],
        )
        frame = object()
        live_results = [
            {
                "name": "Hari",
                "primary": True,
                "stabilized": True,
                "recognized": True,
                "box": {"w": 100, "h": 100},
            }
        ]

        with patch.object(app_module, "resolve_device_id", return_value=device):
            with patch.object(app_module.cameras, "read", return_value=frame):
                with patch.object(
                    app_module.faces, "recognize", return_value=live_results
                ) as recognize:
                    name, state = app_module._camera_identity_snapshot(
                        require_live_face=True, device_id=device
                    )

        recognize.assert_called_once()
        self.assertEqual(name, "Hari")
        self.assertEqual(state, "recognized")

    def test_session_open_does_not_recall_previous_viewer(self) -> None:
        device = "b0a6048ad4e0"
        app_module._remember_voice_viewer("Kartik", device)

        with patch.object(app_module, "resolve_device_id", return_value=device):
            with patch.object(app_module.cameras, "read", return_value=None):
                with patch.object(
                    app_module.faces,
                    "recognize_identity",
                    return_value=(None, "no_face"),
                ):
                    name, state = app_module._session_open_identity_snapshot(device)

        self.assertIsNone(name)
        self.assertEqual(state, "no_face")

    def test_clear_face_cache_on_no_frame(self) -> None:
        device = "b0a6048ad4e0"
        app_module._store_face_cache(device, [{"name": "Kartik", "primary": True}])
        key = app_module._results_device_key(device)
        self.assertIn(key, app_module._latest_results_by_device)

        with patch.object(app_module.cameras, "read", return_value=None):
            app_module._vision_tick_device(device, update_tts=False)

        self.assertNotIn(key, app_module._latest_results_by_device)

    def test_stale_frame_skips_vision_and_clears_object_cache(self) -> None:
        device = "b0a6048ad4e0"
        app_module.objects.clear_device(device)
        app_module.objects._cache[device] = (time.time(), [{"label": "cup"}])

        with patch.object(
            app_module.cameras, "frame_age_seconds", return_value=999.0
        ), patch.object(app_module.cameras, "clear_frame") as clear_frame:
            app_module._vision_tick_device(device, update_tts=False)

        clear_frame.assert_called_once_with(device)
        self.assertEqual(app_module.objects.latest(device), [])


if __name__ == "__main__":
    unittest.main()
