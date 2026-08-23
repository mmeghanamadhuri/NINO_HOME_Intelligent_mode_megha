import unittest

from device_registry import effective_device_display_name


class DeviceDisplayNameTests(unittest.TestCase):
    def test_prefers_custom_name_over_mac(self) -> None:
        name = effective_device_display_name(
            device_id="b0a6048addd4",
            display_name="Living room",
            location_name="",
        )
        self.assertEqual(name, "Living room")

    def test_uses_location_when_display_name_is_mac(self) -> None:
        name = effective_device_display_name(
            device_id="b0a6048addd4",
            display_name="B0:A6:04:8A:DD:D4",
            location_name="Bedroom NiNO",
        )
        self.assertEqual(name, "Bedroom NiNO")

    def test_mac_like_display_name_falls_back_to_formatted_mac(self) -> None:
        name = effective_device_display_name(
            device_id="b0a6048addd4",
            display_name="b0a6048addd4",
            location_name="",
        )
        self.assertEqual(name, "B0:A6:04:8A:DD:D4")


if __name__ == "__main__":
    unittest.main()
