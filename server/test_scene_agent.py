"""Scene agent, register/observe phrases, and spatial reports."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from scene_agent import (
    SceneAgent,
    SceneSnapshot,
    diff_scene_states,
    get_scene_agent,
    is_observe_command,
    is_ok_nino_join,
    is_register_request,
    merge_snapshots,
    reset_scene_agent,
)


class PhraseTests(unittest.TestCase):
    def test_register_phrases(self) -> None:
        for text in (
            "Register me",
            "please register me",
            "initiate registration process",
            "start the registration",
            "can you register me",
            "I want to register",
            "sign me up",
        ):
            self.assertTrue(is_register_request(text), text)
        self.assertFalse(is_register_request("what do you see"))
        self.assertFalse(is_register_request("hello"))

    def test_observe_phrases(self) -> None:
        for text in (
            "you can observe and help",
            "observe and help",
            "start observing",
            "just observe",
            "watch quietly",
        ):
            self.assertTrue(is_observe_command(text), text)
        self.assertFalse(is_observe_command("ok nino, now you can join in"))
        self.assertFalse(is_observe_command("what do you see"))

    def test_ok_nino_join(self) -> None:
        for text in (
            "ok Nino, now you can join in",
            "okay nino, explain the context",
            "ok nino, what were we talking about",
            "Okay Nino, tell me",
        ):
            self.assertTrue(is_ok_nino_join(text), text)
        self.assertFalse(is_ok_nino_join("join in"))
        self.assertFalse(is_ok_nino_join("ok nino"))
        self.assertFalse(is_ok_nino_join("what were we talking about"))
        self.assertTrue(
            is_ok_nino_join("now you can join in", wake_stripped=True)
        )
        self.assertFalse(
            is_ok_nino_join("now you can join in", wake_stripped=False)
        )


class SceneAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_scene_agent("dev1")
        self.agent = SceneAgent(device_id="dev1")
        self.agent.begin_session("Hari", is_guest=False)

    def test_ingest_and_context(self) -> None:
        self.agent.ingest_pose(
            side="left",
            tilt="center",
            names=["Hari"],
            detections=[{"label": "laptop"}],
            emotions={"Hari": "happy"},
            scene_summary="Hari is at a desk",
        )
        block = self.agent.context_block()
        self.assertIn("Hari", block)
        self.assertIn("left", block)
        self.assertEqual(self.agent.user_last_side(), "left")
        self.assertEqual(self.agent.suggest_motion(), ["look_left"])
        self.assertEqual(self.agent.dominant_emotion(), "happy")

    def test_fallback_report_asks_for_help(self) -> None:
        self.agent.ingest_pose(
            side="front",
            names=["Hari"],
            detections=[{"label": "cup"}],
            emotions={"Hari": "sad"},
        )
        report = self.agent._fallback_report(ask_help=True)
        self.assertIn("front", report.lower())
        self.assertIn("How can I help you?", report)

    def test_observe_flag(self) -> None:
        self.assertFalse(self.agent.is_observing())
        self.agent.enter_observe()
        self.assertTrue(self.agent.is_observing())
        self.agent.note_audio("they mentioned tea")
        self.agent.exit_observe()
        self.assertFalse(self.agent.is_observing())
        self.assertIn("tea", self.agent.format_notes())

    def test_compose_uses_fallback_when_llm_fails(self) -> None:
        self.agent.ingest_pose(side="right", names=["Hari"], detections=[])
        with patch("llm_service.ollama_chat", side_effect=RuntimeError("down")):
            report = self.agent.compose_report(ask_help=True)
        self.assertIn("How can I help you?", report)
        self.assertIn("right", report.lower())

    def test_per_device_agents(self) -> None:
        a = get_scene_agent("aa")
        b = get_scene_agent("bb")
        self.assertIsNot(a, b)
        a.enter_observe()
        self.assertTrue(a.is_observing())
        self.assertFalse(b.is_observing())
        reset_scene_agent("aa")
        reset_scene_agent("bb")

    def test_trim_spatial_report_caps_length(self) -> None:
        from scene_agent import trim_spatial_report

        long = "A" * 300 + ". More text here."
        trimmed = trim_spatial_report(long, max_chars=120)
        self.assertLessEqual(len(trimmed), 120)
        self.assertTrue(trimmed.endswith("."))

    def test_fallback_report_is_trimmed(self) -> None:
        self.agent.ingest_pose(
            side="front",
            names=["Hari", "Meghana", "Kartik"],
            detections=[{"label": f"item{i}"} for i in range(12)],
            emotions={"Hari": "happy", "Meghana": "sad", "Kartik": "neutral"},
            scene_summary="A busy room with many things on every surface.",
        )
        report = self.agent._fallback_report(ask_help=True)
        self.assertLessEqual(len(report), 220)
        self.assertIn("How can I help you?", report)

    def test_speaker_mismatch_in_context(self) -> None:
        self.agent.note_speaker(
            viewer_name="Hari",
            voice_name="Hari",
            face_name="Kartik",
            source="face_voice_mismatch_voice",
            score=0.88,
            mismatch=True,
            mismatch_note="I see Kartik but I hear Hari.",
            looking_for=True,
        )
        block = self.agent.context_block()
        self.assertIn("Hari", block)
        self.assertIn("Kartik", block)
        self.assertIn("looking for Hari", block)
        notes = self.agent.format_notes()
        self.assertIn("I see Kartik but I hear Hari", notes)
        self.assertEqual(self.agent.suggest_motion(), ["look_left", "look_right"])
        self.assertEqual(
            self.agent.suggest_eye(reply_path="spatial_report"), "surprised"
        )
        report = self.agent._fallback_report(ask_help=False)
        self.assertIn("I see Kartik but I hear Hari", report)

    def test_sweep_remembers_baseline_and_reports_only_changes(self) -> None:
        self.agent.ingest_pose(
            side="front",
            names=["Hari"],
            detections=[{"label": "cup"}],
        )
        first = self.agent._fallback_report(ask_help=True)
        self.assertIn("Hari", first)
        self.agent._commit_baseline(
            merge_snapshots(
                [
                    SceneSnapshot(
                        side="front",
                        tilt="center",
                        names=["Hari"],
                        detections=[{"label": "cup"}],
                    )
                ]
            )
        )
        self.agent.begin_sweep()
        self.agent.ingest_pose(
            side="front",
            names=["Hari"],
            detections=[{"label": "cup"}],
        )
        report = self.agent.compose_report(ask_help=True)
        self.assertEqual(report, "")

    def test_sweep_reports_new_person_on_second_pass(self) -> None:
        self.agent.ingest_pose(side="front", names=["Hari"], detections=[])
        self.agent._commit_baseline(
            merge_snapshots(
                [SceneSnapshot(side="front", tilt="center", names=["Hari"], detections=[])]
            )
        )
        self.agent.begin_sweep()
        self.agent.ingest_pose(
            side="right",
            names=["Hari", "Meghana"],
            detections=[{"label": "laptop"}],
        )
        delta = diff_scene_states(
            merge_snapshots(
                [SceneSnapshot(side="front", tilt="center", names=["Hari"], detections=[])]
            ),
            self.agent._merge_current_sweep_locked(),
        )
        report = self.agent._fallback_delta_report(delta)
        self.assertIn("Meghana", report)
        self.assertIn("laptop", report.lower())

    def test_begin_session_clears_baseline(self) -> None:
        self.agent._commit_baseline(
            merge_snapshots(
                [SceneSnapshot(side="front", tilt="center", names=["Hari"], detections=[])]
            )
        )
        self.assertTrue(self.agent.has_baseline())
        self.agent.begin_session("Hari", is_guest=False)
        self.assertFalse(self.agent.has_baseline())

    def test_attributed_audio_and_find_speaker(self) -> None:
        self.agent.note_speaker(
            viewer_name="Hari",
            voice_name="Hari",
            face_name=None,
            source="voice",
            score=0.81,
            looking_for=True,
        )
        self.agent.note_audio("they mentioned tea", speaker="Hari")
        notes = self.agent.format_notes()
        self.assertIn("Hari: they mentioned tea", notes)
        self.assertEqual(self.agent.suggest_eye(reply_path="llm"), "curious")
        self.agent.ingest_pose(side="right", names=["Hari"], detections=[])
        self.assertEqual(self.agent.user_last_side(), "right")
        self.assertEqual(self.agent.suggest_motion(), ["look_right"])
        self.assertNotIn("looking for Hari", self.agent.context_block())


if __name__ == "__main__":
    unittest.main()
