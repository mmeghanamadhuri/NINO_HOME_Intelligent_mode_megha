"""Firmware MAC aliases resolve to the discovery-registered robot."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from device_registry import DeviceRegistry, resolve_device_id

CANONICAL = "b0a6048ad4e0"
FIRMWARE = "80f1b2d0ba57"


class DeviceMacAliasTests(unittest.TestCase):
    def test_alias_from_json_resolves_get_and_resolve(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "devices.json"
            path.write_text(
                json.dumps(
                    {
                        "devices": [
                            {
                                "device_id": CANONICAL,
                                "base_url": "http://192.168.0.230",
                                "alternate_macs": [FIRMWARE],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            registry = DeviceRegistry(path)
            self.assertEqual(registry.get(FIRMWARE).device_id, CANONICAL)

            import device_registry as dr

            with patch.object(dr, "_REGISTRY", registry):
                self.assertEqual(resolve_device_id(FIRMWARE), CANONICAL)

    def test_auto_alias_from_client_host_persists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "devices.json"
            path.write_text(
                json.dumps(
                    {
                        "devices": [
                            {
                                "device_id": CANONICAL,
                                "base_url": "http://192.168.0.230",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            registry = DeviceRegistry(path)

            import device_registry as dr

            with patch.object(dr, "_REGISTRY", registry):
                resolved = resolve_device_id(FIRMWARE, client_host="192.168.0.230")
            self.assertEqual(resolved, CANONICAL)
            self.assertEqual(registry.get(FIRMWARE).device_id, CANONICAL)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn(FIRMWARE, saved["devices"][0].get("alternate_macs", []))

    def test_duplicate_ip_rows_are_sanitized_on_load(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "devices.json"
            path.write_text(
                json.dumps(
                    {
                        "devices": [
                            {
                                "device_id": CANONICAL,
                                "base_url": "http://192.168.0.230",
                            },
                            {
                                "device_id": FIRMWARE,
                                "base_url": "http://192.168.0.230",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            registry = DeviceRegistry(path)
            self.assertEqual({d.device_id for d in registry.list_devices()}, {CANONICAL})
            self.assertEqual(registry.get(FIRMWARE).device_id, CANONICAL)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(saved["devices"]), 1)
            self.assertIn(FIRMWARE, saved["devices"][0].get("alternate_macs", []))

    def test_upsert_discovered_aliases_same_ip_instead_of_duplicating(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "devices.json"
            path.write_text(
                json.dumps(
                    {
                        "devices": [
                            {
                                "device_id": CANONICAL,
                                "base_url": "http://192.168.0.230",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            registry = DeviceRegistry(path)
            from device_registry import DeviceRecord

            changed = registry.upsert_discovered(
                [
                    DeviceRecord(
                        device_id=FIRMWARE,
                        display_name="NINO",
                        base_url="http://192.168.0.230",
                    )
                ]
            )
            self.assertEqual(changed, [])
            self.assertEqual({d.device_id for d in registry.list_devices()}, {CANONICAL})
            self.assertEqual(registry.get(FIRMWARE).device_id, CANONICAL)

    def test_ensure_registered_aliases_existing_ip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "devices.json"
            path.write_text(
                json.dumps(
                    {
                        "devices": [
                            {
                                "device_id": CANONICAL,
                                "base_url": "http://192.168.0.230",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            registry = DeviceRegistry(path)
            record = registry.ensure_registered(
                FIRMWARE,
                base_url="http://192.168.0.230",
            )
            self.assertEqual(record.device_id, CANONICAL)
            self.assertEqual({d.device_id for d in registry.list_devices()}, {CANONICAL})


if __name__ == "__main__":
    unittest.main()
