"""Tests for camera emotion overlay integration."""

from __future__ import annotations

import unittest

from emotion_service import EmotionService, _box_iou, _format_emotion_label


class EmotionLabelTests(unittest.TestCase):
    def test_format_emotion(self) -> None:
        self.assertEqual(_format_emotion_label("happy"), "Happy")
        self.assertEqual(_format_emotion_label("uncertain"), "Uncertain")

    def test_box_iou_overlap(self) -> None:
        a = {"x": 10, "y": 10, "w": 100, "h": 100}
        b = (20, 20, 90, 90)
        self.assertGreater(_box_iou(a, b), 0.5)


class FaceAnnotateLabelTests(unittest.TestCase):
    def test_name_emotion_label(self) -> None:
        from face_service import FaceService

        result = {"name": "Chakri", "emotion": "Happy"}
        self.assertEqual(FaceService._overlay_label_text(result), "Chakri | Happy")


class EmotionServiceStatsTests(unittest.TestCase):
    def test_disabled_when_env_off(self) -> None:
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {"EMOTION_ENABLED": "0"}):
            svc = EmotionService()
        self.assertFalse(svc.available)


if __name__ == "__main__":
    unittest.main()
