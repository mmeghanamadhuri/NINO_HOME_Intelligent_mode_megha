"""Session-start greet / register / guest / name-confirm tests."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from face_registration_voice import (
    is_confirm_no,
    is_confirm_yes,
    is_registration_offer_no,
    is_registration_offer_yes,
    is_session_end_utterance,
    parse_spelled_name,
    spell_name_aloud,
)
from session_identity import (
    ASK_NAME_PROMPT,
    GUEST_REPLY,
    OFFER_REGISTER_PROMPT,
    SessionIdentityFlow,
    greet_recognized_user,
    is_guest_name,
    new_guest_name,
)
from voice_service import maybe_address_by_name
import random


class RegistrationOfferTests(unittest.TestCase):
    def test_offer_yes_no(self) -> None:
        for text in ("yes", "Yeah", "sure", "okay", "please"):
            self.assertTrue(is_registration_offer_yes(text), text)
        for text in ("no", "Nope", "skip", "not now", "guest"):
            self.assertTrue(is_registration_offer_no(text), text)
        self.assertFalse(is_registration_offer_yes("My name is Hari"))
        self.assertFalse(is_registration_offer_no("My name is Nora"))

    def test_goodbye_and_stop_end_session_not_guest(self) -> None:
        for text in ("goodbye", "bye", "stop", "please stop"):
            self.assertTrue(is_session_end_utterance(text), text)
        self.assertFalse(is_session_end_utterance("yes"))
        self.assertFalse(is_session_end_utterance("Hari"))

    def test_confirm_yes_good(self) -> None:
        for text in ("yes", "yeah", "that's right", "good", "okay", "correct"):
            self.assertTrue(is_confirm_yes(text), text)
        for text in ("no", "nope", "wrong", "try again"):
            self.assertTrue(is_confirm_no(text), text)

    def test_spell_name(self) -> None:
        self.assertEqual(spell_name_aloud("Hari"), "H, A, R, I")
        self.assertEqual(parse_spelled_name("H A R I"), "Hari")
        self.assertEqual(parse_spelled_name("aitch ay ar eye"), "Hari")
        self.assertIsNone(parse_spelled_name("huh"))


class SessionIdentityFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.faces = MagicMock()
        self.faces.register_sample.return_value = MagicMock()
        self.faces.train.return_value = {"people": 1, "samples": 2}
        self.faces.validate_registration_name.return_value = (True, None)
        self.faces.identify_registered_face.return_value = None
        self.faces.same_person.return_value = False
        self.read_frame = MagicMock(return_value=object())
        self.flow = SessionIdentityFlow(self.faces, self.read_frame)

    def test_recognized_greet(self) -> None:
        result = self.flow.start_session(
            session_id="s1",
            device_id="nino-home",
            identity_name="Hari",
            identity_state="recognized",
        )
        self.assertEqual(result.reply, greet_recognized_user("Hari"))
        self.assertTrue(result.identified)
        self.assertEqual(result.eye_expression, "heart")
        self.assertFalse(self.flow.in_registration())

    def test_unknown_offer_then_no_guest(self) -> None:
        open_result = self.flow.start_session(
            session_id="s1",
            device_id="nino-home",
            identity_name=None,
            identity_state="unknown",
        )
        self.assertEqual(open_result.reply, OFFER_REGISTER_PROMPT)
        self.assertIsNone(open_result.eye_expression)
        self.assertFalse(open_result.identified)
        self.assertTrue(self.flow.in_registration())
        handled = self.flow.handle_voice("no")
        self.assertTrue(handled.handled)
        self.assertEqual(handled.reply, GUEST_REPLY)
        self.assertTrue(is_guest_name(handled.registered_name))
        self.assertFalse(self.flow.in_registration())
        user, guest = self.flow.current_user()
        self.assertTrue(guest)
        self.assertTrue(is_guest_name(user))

    def test_yes_name_spell_confirm(self) -> None:
        self.flow.start_session(
            session_id="s1",
            device_id="nino-home",
            identity_name=None,
            identity_state="no_face",
        )
        self.assertIn(ASK_NAME_PROMPT, self.flow.handle_voice("yes").reply)
        self.assertIn("spell", self.flow.handle_voice("Hari").reply.lower())
        confirm = self.flow.handle_voice("H A R I")
        self.assertIn("H, A, R, I", confirm.reply)
        self.assertIn("Hari", confirm.reply)
        saved = self.flow.handle_voice("yes")
        self.assertTrue(saved.handled)
        self.assertEqual(saved.registered_name, "Hari")
        self.assertIn("All set, Hari", saved.reply)
        self.faces.register_sample.assert_called()

    def test_confirm_no_retries_name(self) -> None:
        self.flow.start_session(
            session_id="s1",
            device_id="nino-home",
            identity_name=None,
            identity_state="unknown",
        )
        self.flow.handle_voice("yes")
        self.flow.handle_voice("Hari")
        self.flow.handle_voice("H A R I")
        retry = self.flow.handle_voice("no")
        self.assertIn("try again", retry.reply.lower())
        self.assertTrue(self.flow.in_registration())

    def test_register_silence_timeout_becomes_guest(self) -> None:
        self.flow.start_session(
            session_id="s1",
            device_id="nino-home",
            identity_name=None,
            identity_state="unknown",
        )
        self.assertTrue(self.flow.in_registration())
        handled = self.flow.timeout_to_guest()
        self.assertTrue(handled.handled)
        self.assertEqual(handled.reply, "")
        self.assertTrue(is_guest_name(handled.registered_name))
        self.assertFalse(self.flow.in_registration())
        user, guest = self.flow.current_user()
        self.assertTrue(guest)
        self.assertTrue(is_guest_name(user))

    def test_timeout_during_name_step_becomes_guest(self) -> None:
        self.flow.start_session(
            session_id="s1",
            device_id="nino-home",
            identity_name=None,
            identity_state="no_face",
        )
        self.flow.handle_voice("yes")
        self.assertTrue(self.flow.in_registration())
        handled = self.flow.timeout_to_guest()
        self.assertTrue(is_guest_name(handled.registered_name))
        self.assertFalse(self.flow.in_registration())

    def test_timeout_after_guest_is_noop(self) -> None:
        self.flow.start_session(
            session_id="s1",
            device_id="nino-home",
            identity_name=None,
            identity_state="unknown",
        )
        self.flow.handle_voice("no")
        self.assertFalse(self.flow.in_registration())
        handled = self.flow.timeout_to_guest()
        self.assertFalse(handled.handled)

    def test_goodbye_during_offer_not_guest(self) -> None:
        self.flow.start_session(
            session_id="s1",
            device_id="nino-home",
            identity_name=None,
            identity_state="unknown",
        )
        handled = self.flow.handle_voice("goodbye")
        self.assertFalse(handled.handled)
        self.assertTrue(self.flow.in_registration())
        self.assertIsNone(self.flow.current_user()[0])


class NameInjectionTests(unittest.TestCase):
    def test_guest_skips_name(self) -> None:
        reply = maybe_address_by_name("How are you?", "Guest-abc", is_guest=True)
        self.assertEqual(reply, "How are you?")

    def test_already_has_name(self) -> None:
        reply = maybe_address_by_name("Hari, the kettle is on.", "Hari")
        self.assertEqual(reply, "Hari, the kettle is on.")

    def test_about_70_percent(self) -> None:
        hits = 0
        total = 400
        for i in range(total):
            out = maybe_address_by_name(
                "The weather is mild today.",
                "Hari",
                rng=random.Random(i),
            )
            if out.startswith("Hari,"):
                hits += 1
        rate = hits / total
        self.assertGreater(rate, 0.55)
        self.assertLess(rate, 0.85)

    def test_new_guest_name_shape(self) -> None:
        self.assertTrue(is_guest_name(new_guest_name()))


if __name__ == "__main__":
    unittest.main()
