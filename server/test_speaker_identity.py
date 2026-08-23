"""Tests for face + voice cross-modal speaker resolution."""

from __future__ import annotations

import unittest

from speaker_identity import resolve_speaker_identity


class SpeakerIdentityTests(unittest.TestCase):
    def test_face_voice_agree(self) -> None:
        result = resolve_speaker_identity(
            face_name="Kartik",
            face_state="recognized",
            voice_name="Kartik",
            voice_score=0.84,
            voice_state="recognized",
        )
        self.assertEqual(result.viewer_name, "Kartik")
        self.assertEqual(result.source, "face_voice_agree")
        self.assertFalse(result.identity_mismatch)
        self.assertFalse(result.trigger_look_scan)

    def test_mismatch_trusts_voice_and_scans(self) -> None:
        result = resolve_speaker_identity(
            face_name="Kartik",
            face_state="recognized",
            voice_name="Hari",
            voice_score=0.88,
            voice_state="recognized",
        )
        self.assertEqual(result.viewer_name, "Hari")
        self.assertTrue(result.identity_mismatch)
        self.assertTrue(result.trigger_look_scan)
        self.assertEqual(result.source, "face_voice_mismatch_voice")

    def test_voice_only_no_face(self) -> None:
        result = resolve_speaker_identity(
            face_name=None,
            face_state="no_face",
            voice_name="Uday",
            voice_score=0.81,
            voice_state="recognized",
        )
        self.assertEqual(result.viewer_name, "Uday")
        self.assertEqual(result.source, "voice")
        self.assertTrue(result.trigger_look_scan)

    def test_face_only_when_no_voice(self) -> None:
        result = resolve_speaker_identity(
            face_name="Kartik",
            face_state="recognized",
            voice_name=None,
            voice_score=0.0,
            voice_state="no_voice",
        )
        self.assertEqual(result.viewer_name, "Kartik")
        self.assertEqual(result.source, "face")

    def test_guest_session_keeps_guest_memory(self) -> None:
        result = resolve_speaker_identity(
            face_name=None,
            face_state="no_face",
            voice_name=None,
            voice_score=0.0,
            voice_state="no_voice",
            session_user="Guest-abc123",
            session_guest=True,
        )
        self.assertEqual(result.viewer_name, "Guest-abc123")
        self.assertIsNone(result.memory_name)
        self.assertEqual(result.source, "guest")

    def test_identified_session_keeps_memory_without_face(self) -> None:
        result = resolve_speaker_identity(
            face_name=None,
            face_state="no_face",
            voice_name=None,
            voice_score=0.0,
            voice_state="no_voice",
            session_user="Hari",
            session_guest=False,
        )
        self.assertEqual(result.viewer_name, "Hari")
        self.assertEqual(result.memory_name, "Hari")
        self.assertEqual(result.source, "session")
        self.assertFalse(result.trigger_look_scan)


if __name__ == "__main__":
    unittest.main()
