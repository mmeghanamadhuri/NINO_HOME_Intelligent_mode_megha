"""Spatial sweep must preserve listen vs speaking on the stream session."""

from __future__ import annotations

import unittest

import app
import esp_playback as playback
import voice_listen_state as vls


class SpatialSweepListenTests(unittest.TestCase):
    def tearDown(self) -> None:
        app.set_spatial_scan_active("nino-home", False)
        playback.clear_device_busy("nino-home")
        vls.mark_session_closed("sess-sweep", "nino-home")

    def test_continue_listen_while_session_open_and_not_speaking(self) -> None:
        vls.mark_session_open("sess-sweep", "nino-home")
        self.assertTrue(
            app.spatial_skip_continue_listen("sess-sweep", "nino-home"),
        )

    def test_no_continue_listen_during_post_tts_grace(self) -> None:
        vls.mark_session_open("sess-sweep", "nino-home")
        vls.mark_tts_playback("sess-sweep", "nino-home", audio_out_seconds=8.0)
        self.assertFalse(
            app.spatial_skip_continue_listen("sess-sweep", "nino-home"),
        )

    def test_no_continue_listen_while_device_busy(self) -> None:
        vls.mark_session_open("sess-sweep", "nino-home")
        playback.mark_device_busy_for(6.0, device_id="nino-home")
        self.assertFalse(
            app.spatial_skip_continue_listen("sess-sweep", "nino-home"),
        )

    def test_spatial_scan_active_tracks_device(self) -> None:
        self.assertFalse(app.spatial_scan_active("nino-home"))
        app.set_spatial_scan_active("nino-home", True)
        self.assertTrue(app.spatial_scan_active("nino-home"))
        app.set_spatial_scan_active("nino-home", False)
        self.assertFalse(app.spatial_scan_active("nino-home"))


if __name__ == "__main__":
    unittest.main()
