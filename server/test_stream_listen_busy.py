"""After TTS, streamed Aux PCM must wait for post-TTS grace before listen."""

from __future__ import annotations

import unittest

import esp_playback as playback
import voice_listen_state as vls


class StreamListenBusyTests(unittest.TestCase):
    def tearDown(self) -> None:
        playback.clear_device_busy("nino-home")
        vls.mark_session_closed("sess-busy", "nino-home")

    def test_echo_tail_is_short(self) -> None:
        self.assertLessEqual(playback.stream_echo_tail_seconds(), 0.5)

    def test_listen_clears_speaking_busy(self) -> None:
        playback.mark_device_busy_for(8.0, device_id="nino-home")
        self.assertTrue(playback.device_busy_speaking("nino-home"))
        playback.clear_device_busy("nino-home")
        self.assertFalse(playback.device_busy_speaking("nino-home"))

    def test_post_tts_grace_blocks_listen_until_playback_finishes(self) -> None:
        vls.mark_session_open("sess-busy", "nino-home")
        vls.mark_tts_playback("sess-busy", "nino-home", audio_out_seconds=6.0)
        self.assertTrue(vls.in_post_tts_grace("sess-busy", "nino-home"))


if __name__ == "__main__":
    unittest.main()
