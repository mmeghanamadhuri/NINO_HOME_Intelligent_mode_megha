"""Tests for on-disk conversation session history."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import conversation_sessions as cs


class ConversationSessionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.patcher = patch.object(cs, "DEFAULT_SESSIONS_DIR", Path(self.tmp.name))
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.link_patcher = patch("user_devices.link_user_device")
        self.link_patcher.start()
        self.addCleanup(self.link_patcher.stop)

    def test_begin_append_end(self) -> None:
        sid = "abc123session"
        cs.begin_session(sid, device_id="nino-home", user_name="Hari")
        cs.append_session_turn(
            sid,
            device_id="nino-home",
            user_name="Hari",
            user_text="what time is it",
            assistant_text="It is three.",
            reply_path="local_time",
        )
        ended = cs.end_session(sid, device_id="nino-home", user_name="Hari")
        self.assertIsNotNone(ended)
        assert ended is not None
        self.assertEqual(len(ended["turns"]), 1)
        self.assertTrue(ended["ended_at"])
        listed = cs.list_sessions_for_user("Hari")
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["session_id"], sid)
        self.assertEqual(listed[0]["turns"], 1)

    def test_bind_moves_to_user_folder(self) -> None:
        sid = "bindsession01"
        cs.begin_session(sid, device_id="nino-home")
        bound = cs.bind_session_user(sid, device_id="nino-home", user_name="Hari")
        self.assertIsNotNone(bound)
        assert bound is not None
        self.assertEqual(bound["user_name"], "Hari")
        listed = cs.list_sessions_for_user("Hari")
        self.assertEqual(listed[0]["session_id"], sid)


if __name__ == "__main__":
    unittest.main()
