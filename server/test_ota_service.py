"""OTA firmware store and MAC-targeted push."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from ota_service import (
    OtaError,
    OtaService,
    looks_like_esp_image,
    sha256_bytes,
)
from user_devices import normalize_device_mac


def _fake_app_image(payload: bytes = b"hello-ota") -> bytes:
    return bytes([0xE9, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]) + payload


class ImageChecks(unittest.TestCase):
    def test_esp_magic(self) -> None:
        self.assertTrue(looks_like_esp_image(_fake_app_image()))
        self.assertFalse(looks_like_esp_image(b"PK\x03\x04not-an-app"))
        self.assertFalse(looks_like_esp_image(b""))


class StoreTests(unittest.TestCase):
    def test_save_and_list(self) -> None:
        with TemporaryDirectory() as tmp:
            svc = OtaService(Path(tmp))
            data = _fake_app_image(b"slot-a")
            rec = svc.save_firmware(data, filename="USB_Camera.bin", label="lab")
            self.assertEqual(rec.size, len(data))
            self.assertEqual(rec.sha256, sha256_bytes(data))
            self.assertEqual(rec.firmware_id, rec.sha256[:16])
            self.assertTrue(svc.bin_path(rec).is_file())
            listed = svc.list_firmware()
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0].firmware_id, rec.firmware_id)
            self.assertEqual(svc.get(rec.firmware_id).sha256, rec.sha256)

    def test_rejects_non_image(self) -> None:
        with TemporaryDirectory() as tmp:
            svc = OtaService(Path(tmp))
            with self.assertRaises(OtaError):
                svc.save_firmware(b"not firmware")


class TriggerTests(unittest.TestCase):
    def test_mac_required(self) -> None:
        with TemporaryDirectory() as tmp:
            svc = OtaService(Path(tmp))
            with self.assertRaises(OtaError):
                svc.trigger_device("nino-home", firmware_id="abc")

    def test_unknown_device(self) -> None:
        with TemporaryDirectory() as tmp:
            svc = OtaService(Path(tmp))
            rec = svc.save_firmware(_fake_app_image())
            with patch("esp_playback.device_base_url", return_value=None):
                with self.assertRaises(OtaError) as ctx:
                    svc.trigger_device("30eda0e34fc4", firmware_id=rec.firmware_id)
            self.assertEqual(ctx.exception.status_code, 404)

    def test_posts_pull_url_to_robot(self) -> None:
        with TemporaryDirectory() as tmp:
            svc = OtaService(Path(tmp))
            rec = svc.save_firmware(_fake_app_image(b"push-me"))
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"ok": True, "started": True}
            with (
                patch("esp_playback.device_base_url", return_value="http://192.168.0.230"),
                patch("ota_service.public_http_base", return_value="http://192.168.0.166:8000"),
                patch("requests.post", return_value=resp) as post,
            ):
                out = svc.trigger_device("30:ED:A0:E3:4F:C4", firmware_id=rec.firmware_id)
            self.assertTrue(out["ok"])
            self.assertEqual(out["device_id"], "30eda0e34fc4")
            post.assert_called_once()
            args, kwargs = post.call_args
            self.assertEqual(args[0], "http://192.168.0.230/ota")
            payload = kwargs["json"]
            self.assertIn(rec.firmware_id, payload["url"])
            self.assertEqual(payload["sha256"], rec.sha256)
            self.assertEqual(payload["size"], rec.size)

    def test_normalize_mac(self) -> None:
        self.assertEqual(normalize_device_mac("30:ED:A0:E3:4F:C4"), "30eda0e34fc4")


if __name__ == "__main__":
    unittest.main()
