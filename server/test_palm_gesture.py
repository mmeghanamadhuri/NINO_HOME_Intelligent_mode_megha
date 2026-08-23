"""Open-palm stop gesture helpers."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from palm_gesture_service import (
    PalmGestureService,
    hand_bbox_frac,
    is_open_palm_landmarks,
    is_raised_wrist_pose,
    request_esp_palm_listen,
)


def _lm(x: float, y: float) -> SimpleNamespace:
    return SimpleNamespace(x=x, y=y)


def _open_palm_landmarks() -> list:
    """Wrist at origin, fingertips farther out than PIP joints."""
    pts = [_lm(0.5, 0.6) for _ in range(21)]
    pts[0] = _lm(0.50, 0.62)
    pts[3] = _lm(0.40, 0.55)
    pts[4] = _lm(0.32, 0.48)
    pts[6] = _lm(0.48, 0.48)
    pts[8] = _lm(0.46, 0.32)
    pts[10] = _lm(0.50, 0.48)
    pts[12] = _lm(0.50, 0.30)
    pts[14] = _lm(0.52, 0.48)
    pts[16] = _lm(0.54, 0.32)
    pts[18] = _lm(0.56, 0.50)
    pts[20] = _lm(0.60, 0.34)
    return pts


class LandmarkTests(unittest.TestCase):
    def test_open_palm_is_detected(self) -> None:
        self.assertTrue(is_open_palm_landmarks(_open_palm_landmarks()))

    def test_fist_is_not_open(self) -> None:
        pts = [_lm(0.50, 0.50) for _ in range(21)]
        pts[0] = _lm(0.50, 0.55)
        for i in range(1, 21):
            pts[i] = _lm(0.50 + 0.01 * (i % 3), 0.52)
        self.assertFalse(is_open_palm_landmarks(pts, min_area=0.0))

    def test_small_hand_is_ignored(self) -> None:
        pts = _open_palm_landmarks()
        for p in pts:
            p.x = 0.50 + (p.x - 0.50) * 0.05
            p.y = 0.50 + (p.y - 0.50) * 0.05
        self.assertLess(hand_bbox_frac(pts), 0.01)
        self.assertFalse(is_open_palm_landmarks(pts, min_area=0.03))

    def test_raised_wrist_pose(self) -> None:
        pts = [(0.0, 0.0, 0.0)] * 11
        pts[6] = (0.55, 0.50, 0.9)
        pts[8] = (0.56, 0.38, 0.9)
        pts[10] = (0.57, 0.22, 0.9)
        self.assertTrue(is_raised_wrist_pose(pts))
        pts[10] = (0.57, 0.80, 0.9)
        self.assertFalse(is_raised_wrist_pose(pts))


class DebounceTests(unittest.TestCase):
    def test_needs_consecutive_hits(self) -> None:
        svc = PalmGestureService()
        svc.enabled = True
        svc.hits_needed = 2
        svc.interval_s = 0.0
        svc.cooldown_s = 0.0
        svc._backend = "mediapipe"
        frame = object()
        with patch.object(svc, "detect_open_palm", return_value=True):
            self.assertFalse(svc.maybe_trigger(frame, "dev"))
            self.assertTrue(svc.maybe_trigger(frame, "dev"))

    def test_miss_resets_hits(self) -> None:
        svc = PalmGestureService()
        svc.enabled = True
        svc.hits_needed = 2
        svc.interval_s = 0.0
        svc.cooldown_s = 0.0
        svc._backend = "mediapipe"
        frame = object()
        with patch.object(svc, "detect_open_palm", side_effect=[True, False, True]):
            self.assertFalse(svc.maybe_trigger(frame, "dev"))
            self.assertFalse(svc.maybe_trigger(frame, "dev"))
            self.assertFalse(svc.maybe_trigger(frame, "dev"))


class RequestTests(unittest.TestCase):
    def test_posts_gesture_listen(self) -> None:
        resp = MagicMock()
        resp.status_code = 200
        with (
            patch(
                "esp_playback.device_base_url",
                return_value="http://192.168.1.10/",
            ),
            patch("requests.post", return_value=resp) as post,
        ):
            ok, err = request_esp_palm_listen("30eda0e34fc4")
        self.assertTrue(ok)
        self.assertIsNone(err)
        post.assert_called_once()
        self.assertEqual(post.call_args.args[0], "http://192.168.1.10/gesture/listen")

    def test_no_device_url(self) -> None:
        with patch("esp_playback.device_base_url", return_value=None):
            ok, err = request_esp_palm_listen("missing")
        self.assertFalse(ok)
        self.assertEqual(err, "no_esp_url")


if __name__ == "__main__":
    unittest.main()
