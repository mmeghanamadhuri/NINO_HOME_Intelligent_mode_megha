"""Tests for camera emotion detection and vision emotion accumulation."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from emotion_service import (
    EMOTION_TO_EYE,
    FERPLUS_LABELS,
    KERAS_FER_LABELS,
    EmotionService,
    SPEAKABLE_EMOTIONS,
    pick_effective_emotion,
)
from pipeline_priority import (
    begin_voice_query,
    end_voice_query,
    vision_emotion_blocked,
)
from vision_emotion_service import VisionEmotionService, _primary_face_for_emotion


class EmotionMappingTests(unittest.TestCase):
    def test_ferplus_label_count(self) -> None:
        self.assertEqual(len(FERPLUS_LABELS), 8)

    def test_keras_label_count(self) -> None:
        self.assertEqual(len(KERAS_FER_LABELS), 7)
        self.assertIn("happy", KERAS_FER_LABELS)
        self.assertIn("neutral", KERAS_FER_LABELS)

    def test_sad_maps_to_eye(self) -> None:
        self.assertEqual(EMOTION_TO_EYE["sad"], "sad")

    def test_happy_speakable(self) -> None:
        self.assertIn("happy", SPEAKABLE_EMOTIONS)
        self.assertNotIn("neutral", SPEAKABLE_EMOTIONS)

    def test_neutral_suppression_promotes_sad(self) -> None:
        label, conf = pick_effective_emotion(
            {"neutral": 0.74, "sad": 0.21, "happy": 0.03}
        )
        self.assertEqual(label, "sad")
        self.assertGreater(conf, 0.2)


class PrimaryFaceTests(unittest.TestCase):
    def test_picks_largest_stabilized_primary(self) -> None:
        results = [
            {
                "primary": True,
                "stabilized": True,
                "name": "Alice",
                "box": {"x": 0, "y": 0, "w": 50, "h": 50},
            },
            {
                "primary": True,
                "stabilized": True,
                "name": "Bob",
                "box": {"x": 0, "y": 0, "w": 120, "h": 120},
            },
        ]
        face = _primary_face_for_emotion(results)
        self.assertIsNotNone(face)
        assert face is not None
        self.assertEqual(face["name"], "Bob")

    def test_uses_candidate_when_not_stabilized(self) -> None:
        results = [
            {
                "primary": True,
                "stabilized": False,
                "recognized": False,
                "candidate_name": "Chakri",
                "candidate_score": 0.55,
                "name": "Unknown",
                "box": {"x": 0, "y": 0, "w": 100, "h": 100},
            }
        ]
        face = _primary_face_for_emotion(results)
        self.assertIsNotNone(face)
        assert face is not None
        self.assertEqual(face["emotion_name"], "Chakri")
        results = [
            {
                "primary": True,
                "stabilized": True,
                "name": "unknown",
                "box": {"x": 0, "y": 0, "w": 100, "h": 100},
            },
        ]
        self.assertIsNone(_primary_face_for_emotion(results))


class PipelinePriorityTests(unittest.TestCase):
    def test_voice_blocks_vision(self) -> None:
        begin_voice_query()
        self.assertTrue(vision_emotion_blocked())
        end_voice_query()


class VisionEmotionAccumTests(unittest.TestCase):
    def test_fires_after_window_with_votes(self) -> None:
        spoken: list[tuple[str, str | None]] = []

        def _speak(text: str, eye: str | None) -> None:
            spoken.append((text, eye))

        emotion = MagicMock()
        emotion.available = True
        emotion.stats.return_value = {"available": True}
        emotion.detect.return_value = {
            "label": "sad",
            "confidence": 0.8,
            "eye_expression": "sad",
            "spoken": "sad",
            "speakable": True,
        }
        emotion.detect_overlay.return_value = {
            "label": "sad",
            "confidence": 0.8,
            "raw_label": "sad",
            "speakable": True,
        }

        svc = VisionEmotionService(emotion, speak_wav=_speak, is_speaker_busy=lambda: False)
        svc._window_min_s = 0.05
        svc._window_max_s = 0.08
        svc._dominance_ratio = 0.3
        svc._cooldown_s = 0.0

        results = [
            {
                "primary": True,
                "stabilized": True,
                "recognized": True,
                "name": "Jane",
                "box": {"x": 10, "y": 10, "w": 80, "h": 80},
            }
        ]
        frame = MagicMock()

        with patch("vision_emotion_service.vision_emotion_blocked", return_value=False):
            with patch(
                "llm_service.empathy_for_detected_emotion",
                return_value="You look a bit down today, Jane.",
            ):
                for _ in range(8):
                    svc.process_frame(frame, results)
                    time.sleep(0.02)

                deadline = time.time() + 2.0
                while not spoken and time.time() < deadline:
                    time.sleep(0.05)

        self.assertGreaterEqual(len(spoken), 1)
        self.assertEqual(spoken[0][1], "sad")


class VisionEmotionDisplayHoldTests(unittest.TestCase):
    def _make_service(self) -> tuple[VisionEmotionService, MagicMock]:
        emotion = MagicMock()
        emotion.available = True
        emotion.stats.return_value = {"available": True}
        emotion.detect.return_value = None
        svc = VisionEmotionService(
            emotion,
            speak_wav=lambda _text, _eye: None,
            is_speaker_busy=lambda: False,
        )
        svc._display_sample_s = 0.1
        svc._display_hold_s = 0.2
        return svc, emotion

    def _face_results(self, name: str = "Jane") -> list[dict]:
        return [
            {
                "primary": True,
                "stabilized": True,
                "recognized": True,
                "name": name,
                "box": {"x": 10, "y": 10, "w": 80, "h": 80},
            }
        ]

    def test_locks_majority_emotion_after_sample_window(self) -> None:
        svc, emotion = self._make_service()
        frame = MagicMock()
        labels = ["happy", "happy", "sad", "happy"]

        with patch("vision_emotion_service.vision_emotion_blocked", return_value=False):
            for label in labels:
                emotion.detect_overlay.return_value = {
                    "label": label,
                    "confidence": 0.9,
                    "raw_label": label,
                    "speakable": label != "neutral",
                }
                svc.process_frame(frame, self._face_results())
                time.sleep(0.03)

            time.sleep(0.12)
            emotion.detect_overlay.return_value = {
                "label": "sad",
                "confidence": 0.95,
                "raw_label": "sad",
                "speakable": True,
            }
            svc.process_frame(frame, self._face_results())

        overlay = svc.latest_overlay()
        self.assertIsNotNone(overlay)
        assert overlay is not None
        self.assertEqual(overlay.get("emotion"), "happy")
        self.assertEqual(overlay.get("display_phase"), "holding")

    def test_keeps_locked_emotion_during_hold_despite_live_change(self) -> None:
        svc, emotion = self._make_service()
        frame = MagicMock()

        with patch("vision_emotion_service.vision_emotion_blocked", return_value=False):
            emotion.detect_overlay.return_value = {
                "label": "happy",
                "confidence": 0.9,
                "raw_label": "happy",
                "speakable": True,
            }
            for _ in range(4):
                svc.process_frame(frame, self._face_results())
                time.sleep(0.03)
            time.sleep(0.12)
            svc.process_frame(frame, self._face_results())

            emotion.detect_overlay.return_value = {
                "label": "angry",
                "confidence": 0.99,
                "raw_label": "angry",
                "speakable": True,
            }
            for _ in range(3):
                svc.process_frame(frame, self._face_results())
                time.sleep(0.02)

        overlay = svc.latest_overlay()
        self.assertIsNotNone(overlay)
        assert overlay is not None
        self.assertEqual(overlay.get("emotion"), "happy")
        self.assertEqual(overlay.get("display_phase"), "holding")


class EmotionServiceInitTests(unittest.TestCase):
    def test_unavailable_without_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"EMOTION_BACKEND": "ferplus"}):
                with patch.object(EmotionService, "_ensure_model", return_value=False):
                    svc = EmotionService(Path(tmp) / "data")
            self.assertFalse(svc.available)

    def test_keras_default_loads_when_weights_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            models = data / "models"
            models.mkdir(parents=True)
            (models / "emotion_model_best.h5").write_bytes(b"x" * 200_000)
            with patch("emotion_model_loader.load_emotion_model") as mock_load:
                mock_load.return_value = MagicMock()
                svc = EmotionService(data)
            self.assertTrue(svc.available)
            mock_load.assert_called_once()


if __name__ == "__main__":
    unittest.main()
