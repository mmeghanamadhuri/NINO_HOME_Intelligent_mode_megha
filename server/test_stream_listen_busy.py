"""After TTS, streamed Aux PCM must be accepted as soon as the device listens."""

from __future__ import annotations

import unittest

import esp_playback as playback


class StreamListenBusyTests(unittest.TestCase):
    def tearDown(self) -> None:
        playback.clear_device_busy("nino-home")

    def test_echo_tail_is_short(self) -> None:
        self.assertLessEqual(playback.stream_echo_tail_seconds(), 0.5)

    def test_listen_clears_speaking_busy(self) -> None:
        playback.mark_device_busy_for(8.0, device_id="nino-home")
        self.assertTrue(playback.device_busy_speaking("nino-home"))
        playback.clear_device_busy("nino-home")
        self.assertFalse(playback.device_busy_speaking("nino-home"))


if __name__ == "__main__":
    unittest.main()
