"""Soak test runner unit tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from intelligent_mode.smoke_tests import SmokeTestResult
from intelligent_mode.soak_test import (
    SoakTestStore,
    _validate_voice_reply,
    parse_soak_unexpected_reply,
    pick_soak_voice_questions,
    resolve_soak_device,
    soak_failures_to_candidates,
    soak_reply_would_pass,
    stale_soak_incident_resolution,
    run_soak_scenarios,
)


class SoakHelperTests(unittest.TestCase):
    def test_random_voice_picks_vary_by_cycle(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "SOAK_VOICE_RANDOM": "1",
                "SOAK_VOICE_ALL_AGES": "1",
                "SOAK_VOICE_QUESTIONS_PER_CYCLE": "6",
            },
            clear=False,
        ):
            a = {q for q, _ in pick_soak_voice_questions(cycle_number=1)}
            b = {q for q, _ in pick_soak_voice_questions(cycle_number=2)}
        self.assertGreaterEqual(len(a), 5)
        self.assertGreaterEqual(len(b), 5)
        # Core checks always included
        self.assertIn("What is 2 plus 2?", a)
        self.assertIn("Can you see anyone in the room?", a)
        self.assertIn("Set a timer for 2 minutes.", a)

    def test_resolve_soak_device_from_snapshot(self) -> None:
        snap = {
            "devices": {
                "devices": [
                    {
                        "device_id": "b0a6048addd4",
                        "display_name": "NiNO Bot",
                        "play_wav_url": "http://192.168.1.148/play_wav",
                    }
                ]
            }
        }
        row = resolve_soak_device(snap)
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row[0], "b0a6048addd4")
        self.assertIn("play_wav", row[1])

    def test_validate_voice_reply_accepts_expected_token(self) -> None:
        ok, _ = _validate_voice_reply(
            reply="Two plus two is four.",
            path="llm",
            out=b"x" * 600,
            expected=("4", "four"),
            live_esp=True,
        )
        self.assertTrue(ok)

    def test_validate_time_reply_via_local_time_path(self) -> None:
        ok, _ = _validate_voice_reply(
            reply="It is 6:31 AM, Sunday, August 23.",
            path="local_time",
            out=b"x" * 600,
            expected=("time", "clock"),
            live_esp=True,
        )
        self.assertTrue(ok)

    def test_validate_joke_reply_via_joke_path(self) -> None:
        reply = (
            'Ha, okay, here we go! I told my computer I needed a break. '
            'It said, "No problem, I\'ll go to sleep."'
        )
        ok, _ = _validate_voice_reply(
            reply=reply,
            path="joke",
            out=b"x" * 600,
            expected=("joke",),
            live_esp=True,
        )
        self.assertTrue(ok)

    def test_soak_reply_would_pass_parses_unexpected_reply_errors(self) -> None:
        error = (
            "[soak:soak:voice:live:1:5:tell_me_a_joke] path=joke unexpected reply="
            "Ha, okay, here we go! I told my computer I needed a break."
        )
        parsed = parse_soak_unexpected_reply(error)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        path, reply = parsed
        self.assertEqual(path, "joke")
        self.assertTrue(soak_reply_would_pass(path=path, reply=reply))

    def test_stale_soak_incident_resolution(self) -> None:
        time_reason = stale_soak_incident_resolution(
            "[soak:soak:voice:live:3:6:what_time_is_it] path=local_time "
            "unexpected reply=It is 6:31 AM, Sunday, August 23."
        )
        self.assertIsNotNone(time_reason)
        joke_reason = stale_soak_incident_resolution(
            "[soak:soak:voice:live:1:5:tell_me_a_joke] path=joke unexpected reply="
            "Ha, okay, here we go! I told my computer I needed a break."
        )
        self.assertIsNotNone(joke_reason)
        wav_reason = stale_soak_incident_resolution(
            "[soak:soak:voice:live:3:4:what_is_photosynthesis] "
            "ESP play_wav failed: WAV too large for ESP (491846 bytes; max 389120)"
        )
        self.assertIsNotNone(wav_reason)

    def test_failures_become_candidates(self) -> None:
        results = [
            SmokeTestResult(
                test_id="soak:voice:0",
                name="soak:voice:0",
                device_id="server",
                subsystem="voice",
                passed=False,
                message="bad reply",
                severity="critical",
                tier=0,
            )
        ]
        cands = soak_failures_to_candidates(results)
        self.assertEqual(len(cands), 1)
        self.assertIn("[soak:", cands[0].error)

    def test_store_persists_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "soak.json"
            store = SoakTestStore(path=path)
            store.mark_running(running=True)
            from intelligent_mode.soak_test import SoakCycleResult

            store.record_cycle(SoakCycleResult(cycle_number=1, passed=5, failed=0, total=5))
            status = store.status()
            self.assertEqual(status["cycles_completed"], 1)


class SoakScenarioTests(unittest.TestCase):
    def _snapshot(self) -> dict:
        return {
            "devices": {"devices": []},
            "cameras": {},
            "bot_runtime": {},
            "faces": {"recognizer_available": True, "detector": "yunet"},
            "face_registration": {"state": "idle"},
            "llm": {"reachable": False, "warning": "down"},
            "memory": {"database_url_set": False},
            "stt": {"provider": "elevenlabs", "loaded": True},
            "tts": {"last_error": ""},
            "discovery": {"last_error": ""},
        }

    @patch("intelligent_mode.soak_test._tts_synthesis_check", return_value=(True, "ok"))
    @patch("intelligent_mode.smoke_tests.probe_bot_snapshot", return_value=(True, "", 200))
    @patch("network_util.voice_ws_url_for_esp", return_value="ws://host/voice-query")
    @patch("intelligent_mode.smoke_tests._http_get_ok", return_value=(True, ""))
    def test_scenarios_run_when_llm_down(
        self,
        _http: object,
        _ws: object,
        _probe: object,
        _tts: object,
    ) -> None:
        results = run_soak_scenarios(self._snapshot())
        ids = {r.test_id for r in results}
        self.assertIn("soak:face_vision", ids)
        self.assertIn("soak:memory", ids)
        self.assertIn("soak:voice:skipped", ids)


if __name__ == "__main__":
    unittest.main()
