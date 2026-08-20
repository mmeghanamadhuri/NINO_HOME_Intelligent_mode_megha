"""Regression tests for device-scoped current-weather support."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch
import json
import tempfile
from pathlib import Path

from device_registry import DeviceRecord, DeviceRegistry
from voice_service import is_weather_question
from weather_service import (
    DeviceLocationUnavailableError,
    WeatherService,
    weather_voice_reply,
)


MAC_A = "30eda0e34fc4"
MAC_B = "aabbccddeeff"


class WeatherServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.device = DeviceRecord(
            device_id=MAC_A,
            display_name="Test NiNO",
            latitude=51.5072,
            longitude=-0.1276,
            location_name="London, UK",
        )

    @patch("weather_service.requests.get")
    def test_current_conditions_are_normalized_and_cached(
        self, get: MagicMock
    ) -> None:
        response = MagicMock()
        response.json.return_value = {
            "timezone": "Europe/London",
            "current": {
                "time": "2026-07-21T19:45",
                "temperature_2m": 22.4,
                "apparent_temperature": 23.1,
                "weather_code": 2,
                "wind_speed_10m": 14.6,
            },
        }
        get.return_value = response
        service = WeatherService(cache_ttl_seconds=300)

        first = service.current_for_device(self.device)
        second = service.current_for_device(self.device)

        self.assertEqual(first["description"], "partly cloudy skies")
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(get.call_count, 1)
        self.assertEqual(
            weather_voice_reply(self.device, first),
            "Right now in London, UK, it is 22 degrees Celsius, feels like 23, "
            "with partly cloudy skies.",
        )

    def test_device_without_coordinates_is_rejected(self) -> None:
        with self.assertRaises(DeviceLocationUnavailableError):
            WeatherService().current_for_device(DeviceRecord(device_id="no-location"))

    @patch("device_registry.logger.warning")
    def test_default_device_id_is_an_alias_for_the_ui_device(self, warning: MagicMock) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "devices.json"
            path.write_text(
                json.dumps({"devices": [{"device_id": MAC_A}]}),
                encoding="utf-8",
            )
            registry = DeviceRegistry(path)

            for _ in range(3):
                self.assertEqual(registry.resolve_or_default("default").device_id, MAC_A)
                self.assertEqual(registry.resolve_or_default("").device_id, MAC_A)

        warning.assert_not_called()

    def test_unknown_name_is_not_remapped_to_another_robot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "devices.json"
            path.write_text(
                json.dumps({"devices": [{"device_id": MAC_A}]}),
                encoding="utf-8",
            )
            registry = DeviceRegistry(path)
            with patch("device_registry.logger.warning") as warning:
                for _ in range(3):
                    self.assertEqual(registry.resolve_or_default("ghost-bot").device_id, "")
                    self.assertEqual(registry.resolve_or_default("nino-home").device_id, "")
                warning.assert_called()
            self.assertEqual(registry.resolve_or_default(MAC_A).device_id, MAC_A)

    def test_resolve_device_id_does_not_resurrect_offline_mac(self) -> None:
        from device_registry import DeviceRegistry, resolve_device_id
        import device_registry as dr

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "devices.json"
            path.write_text(
                json.dumps({"devices": [{"device_id": MAC_A, "base_url": "http://192.168.0.173"}]}),
                encoding="utf-8",
            )
            registry = DeviceRegistry(path)
            with patch.object(dr, "_REGISTRY", registry):
                self.assertEqual(resolve_device_id(MAC_B), "")
                self.assertIsNone(registry.get(MAC_B))
                self.assertEqual(
                    {d.device_id for d in registry.list_devices()},
                    {MAC_A},
                )

    def test_device_lookup_normalizes_mac_case_and_colons(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "devices.json"
            path.write_text(
                json.dumps({"devices": [{"device_id": "AA:BB:CC:DD:EE:FF"}]}),
                encoding="utf-8",
            )
            registry = DeviceRegistry(path)
            self.assertEqual(registry.get("aabbccddeeff").device_id, MAC_B)
            self.assertEqual(registry.resolve_or_default("AA:BB:CC:DD:EE:FF").device_id, MAC_B)

    def test_name_based_registry_rows_are_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "devices.json"
            path.write_text(
                json.dumps(
                    {
                        "devices": [
                            {"device_id": "nino-home", "base_url": "http://192.168.0.83"},
                            {"device_id": "Nino-P4", "base_url": "http://192.168.0.173"},
                            {
                                "device_id": MAC_A,
                                "base_url": "http://192.168.0.90",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            registry = DeviceRegistry(path)
            ids = {record.device_id for record in registry.list_devices()}
            saved = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(ids, {MAC_A})
        self.assertEqual([item["device_id"] for item in saved["devices"]], [MAC_A])

    def test_ensure_registered_requires_a_mac(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "devices.json"
            path.write_text(json.dumps({"devices": []}), encoding="utf-8")
            registry = DeviceRegistry(path)
            with self.assertRaises(ValueError):
                registry.ensure_registered("nino-home")
            record = registry.ensure_registered("30:ED:A0:E3:4F:C4")
            self.assertEqual(record.device_id, MAC_A)
            self.assertEqual(registry.get("30eda0e34fc4").device_id, MAC_A)

    def test_empty_startup_discovery_keeps_persisted_mac_devices(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "devices.json"
            path.write_text(
                json.dumps(
                    {
                        "devices": [
                            {"device_id": MAC_A, "base_url": "http://192.168.0.83"},
                            {"device_id": MAC_B, "base_url": "http://192.168.0.173"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            registry = DeviceRegistry(path)
            changed = registry.replace_with_discovered([])
            ids = {record.device_id for record in registry.list_devices()}

        self.assertEqual(changed, [])
        self.assertEqual(ids, {MAC_A, MAC_B})

    def test_location_is_persisted_in_device_registry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "devices.json"
            path.write_text(
                json.dumps({"devices": [{"device_id": MAC_A}]}),
                encoding="utf-8",
            )
            registry = DeviceRegistry(path)
            updated = registry.set_location(
                MAC_A,
                latitude=51.5072,
                longitude=-0.1276,
                location_name="London, UK",
            )
            saved = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(updated.latitude, 51.5072)
        self.assertEqual(updated.longitude, -0.1276)
        self.assertEqual(updated.location_name, "London, UK")
        self.assertTrue(updated.location_updated_at)
        self.assertEqual(saved["devices"][0]["latitude"], 51.5072)
        self.assertEqual(saved["devices"][0]["longitude"], -0.1276)
        self.assertEqual(saved["devices"][0]["location_name"], "London, UK")

    def test_reported_wifi_network_is_persisted_in_device_registry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "devices.json"
            path.write_text(
                json.dumps({"devices": [{"device_id": MAC_A}]}),
                encoding="utf-8",
            )
            registry = DeviceRegistry(path)
            updated = registry.set_wifi_network(
                MAC_A,
                ssid="NiNO Home",
                bssid="aa:bb:cc:dd:ee:ff",
                rssi=-54,
                channel=6,
            )
            saved = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(updated.wifi_ssid, "NiNO Home")
        self.assertEqual(updated.wifi_bssid, "AA:BB:CC:DD:EE:FF")
        self.assertEqual(updated.wifi_rssi, -54)
        self.assertEqual(updated.wifi_channel, 6)
        self.assertTrue(updated.wifi_updated_at)
        self.assertEqual(saved["devices"][0]["wifi_bssid"], "AA:BB:CC:DD:EE:FF")

    def test_startup_discovery_replaces_unavailable_devices(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "devices.json"
            path.write_text(
                json.dumps(
                    {
                        "devices": [
                            {
                                "device_id": MAC_A,
                                "base_url": "http://192.168.1.10",
                                "camera_rotation": "cw90",
                                "latitude": 51.5072,
                                "longitude": -0.1276,
                            },
                            {
                                "device_id": MAC_B,
                                "base_url": "http://192.168.1.11",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            registry = DeviceRegistry(path)
            registry.replace_with_discovered(
                [
                    DeviceRecord(
                        device_id=MAC_A,
                        base_url="http://192.168.1.20",
                    )
                ]
            )
            saved = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual([record.device_id for record in registry.list_devices()], [MAC_A])
        self.assertEqual(registry.get(MAC_A).base_url, "http://192.168.1.20")
        self.assertEqual(registry.get(MAC_A).camera_rotation, "cw90")
        self.assertEqual(registry.get(MAC_A).latitude, 51.5072)
        self.assertEqual([item["device_id"] for item in saved["devices"]], [MAC_A])


class WeatherVoiceRoutingTests(unittest.TestCase):
    def test_weather_questions_are_detected(self) -> None:
        for text in (
            "What is the weather today?",
            "Will it rain later?",
            "What is the temperature outside?",
        ):
            with self.subTest(text=text):
                self.assertTrue(is_weather_question(text))

    def test_non_weather_questions_are_not_detected(self) -> None:
        self.assertFalse(is_weather_question("What time is it?"))


if __name__ == "__main__":
    unittest.main()
