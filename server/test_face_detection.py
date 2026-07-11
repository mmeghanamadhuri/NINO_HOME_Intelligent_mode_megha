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
        self.svc._embeddings = {}

    def test_blocks_when_session_primary_active(self) -> None:
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
        self.assertFalse(eligible)

    def test_blocks_partial_match_for_registered_db(self) -> None:
        self.svc._embeddings = {"chakri": ("Chakri", np.zeros((1, 128), dtype=np.float32))}
        eligible = self.svc._registration_eligible(
            detection_valid=True,
            recognized=False,
            stabilized=False,
            candidate_score=0.30,
            candidate_name="Chakri",
        )
        self.assertFalse(eligible)

    def test_blocks_near_match(self) -> None:
        eligible = self.svc._registration_eligible(
            detection_valid=True,
            recognized=False,
            stabilized=False,
            candidate_score=0.40,
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


if __name__ == "__main__":
    unittest.main()
