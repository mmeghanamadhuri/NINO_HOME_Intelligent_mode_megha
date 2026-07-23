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


class WeatherServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.device = DeviceRecord(
            device_id="nino-test",
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

    def test_location_is_persisted_in_device_registry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "devices.json"
            path.write_text(
                json.dumps({"devices": [{"device_id": "nino-test"}]}),
                encoding="utf-8",
            )
            registry = DeviceRegistry(path)
            updated = registry.set_location(
                "nino-test",
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
                json.dumps({"devices": [{"device_id": "nino-test"}]}),
                encoding="utf-8",
            )
            registry = DeviceRegistry(path)
            updated = registry.set_wifi_network(
                "nino-test",
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
                                "device_id": "online",
                                "base_url": "http://192.168.1.10",
                                "camera_rotation": "cw90",
                                "latitude": 51.5072,
                                "longitude": -0.1276,
                            },
                            {
                                "device_id": "offline",
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
                        device_id="online",
                        base_url="http://192.168.1.20",
                    )
                ]
            )
            saved = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual([record.device_id for record in registry.list_devices()], ["online"])
        self.assertEqual(registry.get("online").base_url, "http://192.168.1.20")
        self.assertEqual(registry.get("online").camera_rotation, "cw90")
        self.assertEqual(registry.get("online").latitude, 51.5072)
        self.assertEqual([item["device_id"] for item in saved["devices"]], ["online"])


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
