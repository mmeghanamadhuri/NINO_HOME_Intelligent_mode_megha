"""Tests for tightened YuNet face detection quality gates."""

from __future__ import annotations

import unittest

import numpy as np

from face_service import FaceService


def _yunet_row(
    x: float,
    y: float,
    w: float,
    h: float,
    *,
    score: float = 0.9,
) -> np.ndarray:
    """Build a plausible YuNet row with centered landmarks."""
    cx = x + w * 0.5
    eye_y = y + h * 0.38
    nose_y = y + h * 0.58
    mouth_y = y + h * 0.78
    return np.array(
        [
            x,
            y,
            w,
            h,
            x + w * 0.35,
            eye_y,  # right eye
            x + w * 0.65,
            eye_y,  # left eye
            cx,
            nose_y,  # nose
            x + w * 0.38,
            mouth_y,  # right mouth
            x + w * 0.62,
            mouth_y,  # left mouth
            score,
        ],
        dtype=np.float32,
    )


class LandmarkValidationTests(unittest.TestCase):
    def test_plausible_frontal_face(self) -> None:
        row = _yunet_row(10, 20, 100, 120)
        self.assertTrue(FaceService._landmarks_plausible(row, 10, 20, 100, 120))

    def test_rejects_wall_texture_box_without_landmarks(self) -> None:
        row = _yunet_row(50, 40, 80, 90, score=0.72)
        row[4] = 200.0  # eyes far outside box
        row[6] = 220.0
        self.assertFalse(FaceService._landmarks_plausible(row, 50, 40, 80, 90))

    def test_rejects_mouth_above_nose(self) -> None:
        row = _yunet_row(10, 20, 100, 120)
        row[11] = row[9] - 10
        row[13] = row[9] - 10
        self.assertFalse(FaceService._landmarks_plausible(row, 10, 20, 100, 120))

    def test_rejects_eyes_too_far_apart(self) -> None:
        row = _yunet_row(10, 20, 100, 120)
        row[4] = 5
        row[6] = 250
        self.assertFalse(FaceService._landmarks_plausible(row, 10, 20, 100, 120))


class RegistrationEligibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = FaceService.__new__(FaceService)
        self.svc.match_soft_threshold = 0.39
        self.svc._session_primary_name = None
        self.svc._session_primary_at = 0.0
        self.svc.session_primary_hold_seconds = 90.0
        self.svc._lock = __import__("threading").Lock()
        self.svc._state_lock = __import__("threading").Lock()
        self.svc._tracks = {}
        self.svc._embeddings = {}

    def test_session_primary_does_not_block_unknown(self) -> None:
        import time

        self.svc._session_primary_name = "Chakri"
        self.svc._session_primary_at = time.time()
        eligible = self.svc._registration_eligible(
            detection_valid=True,
            recognized=False,
            stabilized=False,
            candidate_score=0.26,
            candidate_name="Chakri",
        )
        self.assertTrue(eligible)

    def test_allows_partial_match_below_soft_threshold(self) -> None:
        """Unknown @ 0.34 (e.g. vs Dimple) must still be voice-registration eligible."""
        self.svc._embeddings = {"dimple": ("Dimple", np.zeros((1, 128), dtype=np.float32))}
        self.svc.match_soft_threshold = 0.42
        eligible = self.svc._registration_eligible(
            detection_valid=True,
            recognized=False,
            stabilized=False,
            candidate_score=0.343,
            candidate_name="Dimple",
        )
        self.assertTrue(eligible)

    def test_blocks_near_soft_match(self) -> None:
        eligible = self.svc._registration_eligible(
            detection_valid=True,
            recognized=False,
            stabilized=False,
            candidate_score=0.40,
            candidate_name="Alex",
        )
        self.assertFalse(eligible)

    def test_blocks_recognized(self) -> None:
        eligible = self.svc._registration_eligible(
            detection_valid=True,
            recognized=True,
            stabilized=True,
            candidate_score=0.80,
            candidate_name="Alex",
        )
        self.assertFalse(eligible)

    def test_allows_low_score_stranger(self) -> None:
        eligible = self.svc._registration_eligible(
            detection_valid=True,
            recognized=False,
            stabilized=False,
            candidate_score=0.12,
            candidate_name="Alex",
        )
        self.assertTrue(eligible)

    def test_primary_viewer_can_use_pending_overlay_name(self) -> None:
        pending = [
            {
                "primary": True,
                "recognized": False,
                "stabilized": False,
                "pending": True,
                "name": "Hari",
                "candidate_name": "Hari",
                "box": {"x": 0, "y": 0, "w": 80, "h": 80},
            }
        ]
        self.assertIsNone(self.svc.primary_viewer(pending))
        self.assertEqual(
            self.svc.primary_viewer(pending, allow_pending=True),
            "Hari",
        )


if __name__ == "__main__":
    unittest.main()
