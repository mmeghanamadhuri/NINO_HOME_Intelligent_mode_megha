"""Session-start greet / register / guest / name-confirm tests."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from face_registration_voice import (
    is_already_registered_claim,
    is_confirm_no,
    is_confirm_yes,
    is_registration_offer_no,
    is_registration_offer_yes,
    is_registration_stop_process,
    is_session_end_utterance,
    parse_spelled_name,
    spell_name_aloud,
)
from session_identity import (
    ASK_NAME_PROMPT,
    GUEST_REPLY,
    NO_FACE_GUEST_REPLY,
    OFFER_REGISTER_PROMPT,
    STILL_UNKNOWN_REPLY,
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

    def test_not_new_user_claim(self) -> None:
        for text in (
            "I'm not a new user",
            "I am not new",
            "I'm already registered",
            "you already know me",
            "you know who I am",
        ):
            self.assertTrue(is_already_registered_claim(text), text)
        self.assertFalse(is_already_registered_claim("no"))
        self.assertFalse(is_already_registered_claim("My name is Hari"))

    def test_stop_process_is_registration_abort(self) -> None:
        for text in ("cancel", "stop", "stop the process", "please cancel"):
            self.assertTrue(is_registration_stop_process(text), text)
        self.assertFalse(is_registration_stop_process("no"))
        self.assertFalse(is_registration_stop_process("goodbye"))

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
        self.faces.recognize_identity.return_value = (None, "no_face")
        self.read_frame = MagicMock(return_value=object())
        self.flow = SessionIdentityFlow(self.faces, self.read_frame)

    def test_recognized_greet(self) -> None:
        result = self.flow.start_session(
            session_id="s1",
            device_id="30eda0e34fc4",
            identity_name="Hari",
            identity_state="recognized",
        )
        self.assertEqual(result.reply, greet_recognized_user("Hari"))
        self.assertTrue(result.identified)
        self.assertEqual(result.eye_expression, "heart")
        self.assertFalse(self.flow.in_registration())
        self.assertNotIn("new user", result.reply.lower())
        self.assertEqual(result.reply_path, "session_greet")
        user, guest = self.flow.current_user()
        self.assertEqual(user, "Hari")
        self.assertFalse(guest)

    def test_identified_greet_echo_is_skipped(self) -> None:
        self.flow.start_session(
            session_id="s1",
            device_id="30eda0e34fc4",
            identity_name="Hari",
            identity_state="recognized",
        )
        self.assertTrue(
            self.flow.should_skip_prompt_echo("Hey Hari, how can I help you")
        )
        self.assertTrue(self.flow.should_skip_prompt_echo("Hey Hari"))
        self.assertTrue(self.flow.should_skip_prompt_echo("Good morning Hari"))
        self.assertFalse(self.flow.should_skip_prompt_echo("What's the weather?"))

    def test_unknown_offer_then_no_guest(self) -> None:
        open_result = self.flow.start_session(
            session_id="s1",
            device_id="30eda0e34fc4",
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
            device_id="30eda0e34fc4",
            identity_name=None,
            identity_state="unknown",
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
            device_id="30eda0e34fc4",
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
            device_id="30eda0e34fc4",
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
            device_id="30eda0e34fc4",
            identity_name=None,
            identity_state="unknown",
        )
        self.flow.handle_voice("yes")
        self.assertTrue(self.flow.in_registration())
        handled = self.flow.timeout_to_guest()
        self.assertTrue(is_guest_name(handled.registered_name))
        self.assertFalse(self.flow.in_registration())

    def test_timeout_after_guest_is_noop(self) -> None:
        self.flow.start_session(
            session_id="s1",
            device_id="30eda0e34fc4",
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
            device_id="30eda0e34fc4",
            identity_name=None,
            identity_state="unknown",
        )
        handled = self.flow.handle_voice("goodbye")
        self.assertFalse(handled.handled)
        self.assertTrue(self.flow.in_registration())
        self.assertIsNone(self.flow.current_user()[0])

    def test_no_face_after_hunt_is_guest(self) -> None:
        open_result = self.flow.start_session(
            session_id="s1",
            device_id="30eda0e34fc4",
            identity_name=None,
            identity_state="no_face",
        )
        self.assertEqual(open_result.reply, NO_FACE_GUEST_REPLY)
        self.assertTrue(open_result.is_guest)
        self.assertFalse(self.flow.in_registration())
        user, guest = self.flow.current_user()
        self.assertTrue(guest)
        self.assertTrue(is_guest_name(user))

    def test_stop_during_offer_becomes_guest(self) -> None:
        self.flow.start_session(
            session_id="s1",
            device_id="30eda0e34fc4",
            identity_name=None,
            identity_state="unknown",
        )
        handled = self.flow.handle_voice("stop")
        self.assertTrue(handled.handled)
        self.assertEqual(handled.reply, GUEST_REPLY)
        self.assertFalse(self.flow.in_registration())
        self.assertTrue(self.flow.current_user()[1])

    def test_cancel_during_name_becomes_guest(self) -> None:
        self.flow.start_session(
            session_id="s1",
            device_id="30eda0e34fc4",
            identity_name=None,
            identity_state="unknown",
        )
        self.flow.handle_voice("yes")
        handled = self.flow.handle_voice("cancel the process")
        self.assertTrue(handled.handled)
        self.assertEqual(handled.reply, GUEST_REPLY)
        self.assertTrue(self.flow.current_user()[1])

    def test_not_new_user_identified_cancels_register(self) -> None:
        self.faces.recognize_identity.return_value = ("Hari", "recognized")
        self.flow.start_session(
            session_id="s1",
            device_id="30eda0e34fc4",
            identity_name=None,
            identity_state="unknown",
        )
        handled = self.flow.handle_voice("I'm not a new user")
        self.assertTrue(handled.handled)
        self.assertEqual(handled.reply, greet_recognized_user("Hari"))
        self.assertEqual(handled.registered_name, "Hari")
        self.assertFalse(self.flow.in_registration())
        user, guest = self.flow.current_user()
        self.assertEqual(user, "Hari")
        self.assertFalse(guest)

    def test_not_new_user_still_unknown_stays_in_register(self) -> None:
        self.faces.recognize_identity.return_value = (None, "unknown")
        self.flow.start_session(
            session_id="s1",
            device_id="30eda0e34fc4",
            identity_name=None,
            identity_state="unknown",
        )
        handled = self.flow.handle_voice("you already know me")
        self.assertTrue(handled.handled)
        self.assertEqual(handled.reply, STILL_UNKNOWN_REPLY)
        self.assertTrue(self.flow.in_registration())

    def test_confirm_no_still_retries_not_guest(self) -> None:
        self.flow.start_session(
            session_id="s1",
            device_id="30eda0e34fc4",
            identity_name=None,
            identity_state="unknown",
        )
        self.flow.handle_voice("yes")
        self.flow.handle_voice("Hari")
        self.flow.handle_voice("H A R I")
        retry = self.flow.handle_voice("no")
        self.assertIn("try again", retry.reply.lower())
        self.assertTrue(self.flow.in_registration())

    def test_apply_visible_scene_switches_user(self) -> None:
        self.flow.start_session(
            session_id="s1",
            device_id="30eda0e34fc4",
            identity_name="Hari",
            identity_state="recognized",
        )
        result = self.flow.apply_visible_scene(
            visible_names=["Nora"], scene_state="recognized"
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.user_name, "Nora")
        self.assertEqual(result.reply, "")
        self.assertTrue(result.identified)
        self.assertEqual(self.flow.current_user()[0], "Nora")

    def test_apply_visible_scene_unknown_starts_register(self) -> None:
        self.flow.start_session(
            session_id="s1",
            device_id="30eda0e34fc4",
            identity_name="Hari",
            identity_state="recognized",
        )
        result = self.flow.apply_visible_scene(
            visible_names=[], scene_state="unknown"
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.reply, OFFER_REGISTER_PROMPT)
        self.assertTrue(self.flow.in_registration())

    def test_apply_visible_scene_no_face_becomes_guest(self) -> None:
        self.flow.start_session(
            session_id="s1",
            device_id="30eda0e34fc4",
            identity_name="Hari",
            identity_state="recognized",
        )
        result = self.flow.apply_visible_scene(
            visible_names=[], scene_state="no_face"
        )
        self.assertIsNotNone(result)
        self.assertTrue(result.is_guest)
        self.assertTrue(is_guest_name(result.user_name))
        self.assertEqual(result.reply, "")

    def test_apply_visible_scene_keeps_same_user(self) -> None:
        self.flow.start_session(
            session_id="s1",
            device_id="30eda0e34fc4",
            identity_name="Hari",
            identity_state="recognized",
        )
        result = self.flow.apply_visible_scene(
            visible_names=["Hari"], scene_state="recognized"
        )
        self.assertIsNone(result)
        self.assertEqual(self.flow.current_user()[0], "Hari")

    def test_apply_visible_scene_allow_register_false_skips_offer(self) -> None:
        self.flow.start_session(
            session_id="s1",
            device_id="30eda0e34fc4",
            identity_name="Hari",
            identity_state="recognized",
        )
        result = self.flow.apply_visible_scene(
            visible_names=[], scene_state="unknown", allow_register=False
        )
        self.assertIsNone(result)
        self.assertFalse(self.flow.in_registration())
        self.assertEqual(self.flow.current_user()[0], "Hari")

    def test_apply_visible_scene_allow_register_false_still_switches_user(self) -> None:
        self.flow.start_session(
            session_id="s1",
            device_id="30eda0e34fc4",
            identity_name="Hari",
            identity_state="recognized",
        )
        result = self.flow.apply_visible_scene(
            visible_names=["Nora"], scene_state="recognized", allow_register=False
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.user_name, "Nora")
        self.assertEqual(result.reply, "")
        self.assertEqual(self.flow.current_user()[0], "Nora")

    def test_apply_visible_scene_allow_register_false_becomes_guest(self) -> None:
        self.flow.start_session(
            session_id="s1",
            device_id="30eda0e34fc4",
            identity_name="Hari",
            identity_state="recognized",
        )
        result = self.flow.apply_visible_scene(
            visible_names=[], scene_state="no_face", allow_register=False
        )
        self.assertIsNotNone(result)
        self.assertTrue(result.is_guest)
        self.assertEqual(result.reply, "")
        self.assertTrue(is_guest_name(result.user_name))

    def test_apply_visible_scene_allow_register_true_still_offers(self) -> None:
        self.flow.start_session(
            session_id="s1",
            device_id="30eda0e34fc4",
            identity_name="Hari",
            identity_state="recognized",
        )
        result = self.flow.apply_visible_scene(
            visible_names=[], scene_state="unknown", allow_register=True
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.reply, OFFER_REGISTER_PROMPT)
        self.assertTrue(self.flow.in_registration())


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


class PerDeviceIdentityTests(unittest.TestCase):
    def test_robots_do_not_share_register_state(self) -> None:
        from session_identity import configure_session_identity, get_session_identity

        configure_session_identity(MagicMock(), lambda: None)
        a = get_session_identity("588c81542a4c")
        b = get_session_identity("b0a6048addd4")
        self.assertIsNot(a, b)
        a.start_session(
            session_id="a",
            device_id="588c81542a4c",
            identity_name=None,
            identity_state="unknown",
        )
        self.assertTrue(a.in_registration())
        self.assertFalse(b.in_registration())

    def test_new_mac_uses_that_robot_frame_getter(self) -> None:
        from session_identity import configure_session_identity, get_session_identity

        seen: list[str | None] = []

        def factory(device_id: str | None):
            def _read():
                seen.append(device_id)
                return None

            return _read

        configure_session_identity(MagicMock(), lambda: "ui-frame", factory)
        flow = get_session_identity("588c81542a4c")
        self.assertIsNotNone(flow)
        flow._read_frame()
        self.assertEqual(seen, ["588c81542a4c"])


if __name__ == "__main__":
    unittest.main()
