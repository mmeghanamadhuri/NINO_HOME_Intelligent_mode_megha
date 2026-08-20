"""Users can own many device MACs; ids stay 12 lowercase hex."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import user_devices as ud


class UserDeviceMacTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "user_devices.json"
        self.patcher = patch.object(ud, "DEFAULT_PATH", self.path)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_normalize_colon_and_case(self) -> None:
        self.assertEqual(ud.normalize_device_mac("30:ED:A0:E3:4F:C4"), "30eda0e34fc4")
        self.assertEqual(ud.normalize_device_mac("30eda0e34fc4"), "30eda0e34fc4")
        self.assertEqual(ud.normalize_device_mac("30-ed-a0-e3-4f-c4"), "30eda0e34fc4")
        self.assertEqual(ud.normalize_device_mac("nino-a0e34fc4"), "")
        self.assertEqual(ud.normalize_device_mac("not-a-mac"), "")
        self.assertEqual(ud.normalize_device_mac(""), "")

    def test_pretty_print(self) -> None:
        self.assertEqual(ud.format_device_mac("30eda0e34fc4"), "30:ED:A0:E3:4F:C4")
        self.assertEqual(ud.format_device_mac("30:ed:a0:e3:4f:c4"), "30:ED:A0:E3:4F:C4")
        self.assertEqual(ud.format_device_mac("nino-home"), "")

    def test_canonical_rejects_non_mac(self) -> None:
        self.assertEqual(ud.canonical_device_id("30:ED:A0:E3:4F:C4"), "30eda0e34fc4")
        self.assertEqual(ud.canonical_device_id("nino-home"), "")
        self.assertEqual(ud.canonical_device_id("Nino-P4"), "")

    def test_link_and_lookup(self) -> None:
        ud.link_user_device("Hari", "30:ED:A0:E3:4F:C4")
        self.assertEqual(ud.devices_for_user("Hari"), ["30eda0e34fc4"])
        self.assertEqual(ud.users_for_device("30eda0e34fc4"), ["Hari"])
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(payload["users"]["Hari"]["macs"], ["30eda0e34fc4"])

    def test_one_user_many_macs(self) -> None:
        ud.link_user_device("Hari", "30eda0e34fc4")
        ud.link_user_device("Hari", "AABBCCDDEEFF")
        ud.link_user_device("Hari", "30:ED:A0:E3:4F:C4")
        self.assertEqual(
            ud.devices_for_user("Hari"),
            ["30eda0e34fc4", "aabbccddeeff"],
        )
        self.assertEqual(ud.users_for_device("aa:bb:cc:dd:ee:ff"), ["Hari"])

    def test_skips_guest_and_unknown(self) -> None:
        ud.link_user_device("guest", "30eda0e34fc4")
        ud.link_user_device("guest-2", "aabbccddeeff")
        ud.link_user_device("unknown", "112233445566")
        ud.link_user_device("Face", "778899aabbcc")
        ud.link_user_device("Hari", "nino-home")
        self.assertEqual(ud.devices_for_user("guest"), [])
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
