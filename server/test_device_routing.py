"""MAC routing: camera, speaker, and face tracks stay on the owning robot."""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

from camera import CameraPool
from device_registry import DeviceRecord
from esp_playback import deliver_wav_to_device
from face_service import FaceService

MAC_A = "588c81542a4c"
MAC_B = "b0a6048addd4"


def _registry_with_two_robots() -> MagicMock:
    rec_a = DeviceRecord(device_id=MAC_A, display_name="Gitam")
    rec_b = DeviceRecord(
        device_id=MAC_B,
        display_name="Vaseekaran",
        base_url="http://192.168.0.173",
        camera_url="http://192.168.0.173/stream",
        play_wav_url="http://192.168.0.173/play_wav",
    )
    registry = MagicMock()
    mapping = {MAC_A: rec_a, MAC_B: rec_b}

    def _get(device_id):
        return mapping.get(str(device_id or "").strip().lower())

    registry.get.side_effect = _get
    registry.resolve_or_default.side_effect = lambda device_id: mapping.get(
        str(device_id or "").strip().lower(), rec_b
    )
    return registry


class CameraRoutingTests(unittest.TestCase):
    def test_mac_without_camera_does_not_read_another_robot(self) -> None:
        registry = _registry_with_two_robots()
        with patch("device_registry.get_device_registry", return_value=registry):
            pool = CameraPool()
            self.assertIsNone(pool.read(MAC_A))
            with self.assertRaises(RuntimeError):
                pool.ensure(MAC_A)


class PlaybackRoutingTests(unittest.TestCase):
    def test_named_mac_does_not_fall_back_to_another_robot_url(self) -> None:
        registry = _registry_with_two_robots()
        wav = b"RIFF" + b"\x00" * 40
        with patch("device_registry.get_device_registry", return_value=registry):
            with patch("esp_playback.esp_play_wav_url", return_value="http://stolen/play_wav"):
                with self.assertRaises(RuntimeError) as raised:
                    deliver_wav_to_device(MAC_A, wav)
        self.assertIn(MAC_A, str(raised.exception))

    def test_named_mac_posts_only_to_its_own_play_url(self) -> None:
        registry = _registry_with_two_robots()
        wav = b"RIFF" + b"\x00" * 40
        with patch("device_registry.get_device_registry", return_value=registry):
            with patch("esp_playback._post_wav_to_url") as post:
                deliver_wav_to_device(MAC_B, wav)
        post.assert_called_once()
        self.assertEqual(post.call_args.args[0], "http://192.168.0.173/play_wav")
        self.assertEqual(post.call_args.kwargs["device_id"], MAC_B)


class FaceTrackRoutingTests(unittest.TestCase):
    def test_stabilizer_state_is_per_mac(self) -> None:
        svc = FaceService.__new__(FaceService)
        svc._state_lock = threading.Lock()
        svc._tracks = {}
        svc._track(MAC_A).stable_name = "Kartik"
        svc._track(MAC_A).session_primary_name = "Kartik"
        self.assertEqual(svc._track(MAC_A).stable_name, "Kartik")
        self.assertIsNone(svc._track(MAC_B).stable_name)
        self.assertIsNone(svc._track(MAC_B).session_primary_name)


class LanMacLookupTests(unittest.TestCase):
    def test_lookup_lan_mac_reads_complete_arp_row(self) -> None:
        from device_discovery import lookup_lan_mac

        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
            handle.write(
                "IP address       HW type     Flags       HW address            Mask     Device\n"
                "192.168.0.173    0x1         0x2         b0:a6:04:8a:dd:d4     *        wlan0\n"
                "192.168.0.83     0x1         0x0         00:00:00:00:00:00     *        wlan0\n"
            )
            path = handle.name
        try:
            self.assertEqual(lookup_lan_mac("192.168.0.173", arp_path=path), MAC_B)
            self.assertEqual(lookup_lan_mac("192.168.0.83", arp_path=path), "")
        finally:
            os.unlink(path)

    def test_missing_status_mac_uses_lan_neighbor(self) -> None:
        from device_discovery import DeviceDiscovery

        payload = {
            "ok": True,
            "device_name": "NINO - HOME Vaseekaran",
            "device_id": "000000000000",
            "ip": "192.168.0.173",
        }
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = payload
        discovery = DeviceDiscovery(registry=MagicMock())
        with patch("device_discovery.requests.get", return_value=response):
            with patch("device_discovery.lookup_lan_mac", return_value=MAC_B):
                records = discovery._records_from_status({("192.168.0.173", 80): None})
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].device_id, MAC_B)
        self.assertEqual(records[0].effective_base_url(), "http://192.168.0.173")


if __name__ == "__main__":
    unittest.main()
