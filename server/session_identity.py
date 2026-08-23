"""Session-start face greeting and in-stream registration (guest / name / spell)."""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Literal

from face_registration_service import (
    FaceRegVoiceResult,
    capture_face_samples,
    already_registered_reply,
    same_person_refresh_reply,
)
from face_registration_voice import (
    is_already_registered_claim,
    is_confirm_no,
    is_confirm_yes,
    is_face_reg_prompt_echo,
    is_opening_greeting_echo,
    is_unconditional_greet_echo,
    is_registration_cancel,
    is_registration_offer_no,
    is_registration_offer_yes,
    is_registration_stop_process,
    is_session_goodbye_utterance,
    parse_misidentification_denial,
    parse_next_spell_letter,
    parse_spelled_name,
    spell_name_aloud,
)
from stream_asr import DEFAULT_REGISTER_MAX_MS, DEFAULT_MAX_MS

logger = logging.getLogger(__name__)

SessionIdentState = Literal[
    "idle",
    "offer_register",
    "awaiting_spell",
    "awaiting_letter_confirm",
    "awaiting_confirm",
    "guest",
    "identified",
]

OFFER_REGISTER_PROMPT = "Looks like you are a new user, can I register you"
ASK_SPELL_PROMPT = "Please spell your name, one letter at a time."
GUEST_REPLY = "No problem. I'll keep this as a guest chat. How can I help you?"
NO_FACE_GUEST_REPLY = "How can I help you"
STILL_UNKNOWN_REPLY = (
    "I still don't recognize you. Looks like you are a new user, can I register you"
)
MISIDENTIFY_APOLOGY = (
    "Sorry, I got the wrong person. Let me look at the camera again."
)
MISIDENTIFY_STILL_UNKNOWN = (
    "I still don't see a match. Would you like to register, or tell me your name?"
)
# No activity during register → guest and keep the session.
REGISTER_SILENCE_MS = DEFAULT_REGISTER_MAX_MS
# After at least one letter, this much silence means spelling is finished.
SPELL_LETTER_IDLE_MS = 5000
SPELL_RETRY_PROMPT = "I didn't catch that letter. Please say the next letter."
LETTER_AGAIN_PROMPT = "Sorry, please say that letter again."
CONFIRM_RETRY_PROMPT = "Okay, let's start over. Please spell your name, one letter at a time."


def greet_recognized_user(name: str) -> str:
    cleaned = (name or "").strip()
    if not cleaned:
        return "Hey, how can I help you"
    return f"Hey {cleaned}, how can I help you"


def confirm_name_prompt(name: str) -> str:
    letters = spell_name_aloud(name)
    return f"That's {letters}. Is that right?"


def confirm_letter_prompt(letter: str) -> str:
    cleaned = (letter or "").strip().upper()[:1]
    return f"Is it {cleaned}?" if cleaned else SPELL_RETRY_PROMPT


def new_guest_name() -> str:
    return f"Guest-{uuid.uuid4().hex[:6]}"


def is_guest_name(name: str | None) -> bool:
    raw = (name or "").strip()
    return raw.lower().startswith("guest-") or raw.lower() == "guest"


@dataclass
class SessionOpenResult:
    reply: str
    reply_path: str
    user_name: str | None = None
    is_guest: bool = False
    eye_expression: str | None = None
    identified: bool = False


class SessionIdentityFlow:
    """Per-device stream-session identity: greet, offer register, guest, or save."""

    def __init__(self, faces: Any, read_frame: Callable[[], Any]) -> None:
        self._faces = faces
        self._read_frame = read_frame
        self._lock = threading.Lock()
        self._state: SessionIdentState = "idle"
        self._pending_name: str = ""
        self._pending_letters: list[str] = []
        self._pending_letter: str = ""
        self._user_name: str | None = None
        self._is_guest = False
        self._session_id = ""
        self._device_id = ""
        self._active = False
        self._opening_echo_budget = 0
        self._declined_register = False

    def set_frame_getter(self, read_frame: Callable[[], Any]) -> None:
        self._read_frame = read_frame

    def is_active(self) -> bool:
        with self._lock:
            return self._active

    def current_user(self) -> tuple[str | None, bool]:
        with self._lock:
            return self._user_name, self._is_guest

    def in_registration(self) -> bool:
        with self._lock:
            return self._state in {
                "offer_register",
                "awaiting_spell",
                "awaiting_letter_confirm",
                "awaiting_confirm",
            }

    def registration_listen_max_ms(self) -> int:
        """VAD listen cap while registering: 5s after a letter, else 30s."""
        with self._lock:
            if self._state == "awaiting_spell" and self._pending_letters:
                return SPELL_LETTER_IDLE_MS
            if self._state in {
                "offer_register",
                "awaiting_spell",
                "awaiting_letter_confirm",
                "awaiting_confirm",
            }:
                return REGISTER_SILENCE_MS
        return DEFAULT_MAX_MS

    def _clear_pending_spelling(self) -> None:
        self._pending_name = ""
        self._pending_letters = []
        self._pending_letter = ""

    def _name_from_letters(self) -> str:
        letters = [ch for ch in self._pending_letters if ch.isalpha()]
        return "".join(letters).title() if letters else ""

    def start_session(
        self,
        *,
        session_id: str,
        device_id: str,
        identity_name: str | None,
        identity_state: str,
    ) -> SessionOpenResult:
        with self._lock:
            self._active = True
            self._session_id = session_id
            self._device_id = device_id
            self._clear_pending_spelling()
            self._is_guest = False
            self._declined_register = False
            self._opening_echo_budget = 2
            name = (identity_name or "").strip()
            if identity_state == "recognized" and name and name.lower() not in {
                "unknown",
                "face",
            }:
                self._state = "identified"
                self._user_name = name
                logger.info(
                    "Session identity: identified greet name=%s session=%s",
                    name,
                    session_id,
                )
                return SessionOpenResult(
                    reply=greet_recognized_user(name),
                    reply_path="session_greet",
                    user_name=name,
                    eye_expression="heart",
                    identified=True,
                )
            if identity_state == "no_face":
                guest = new_guest_name()
                self._state = "guest"
                self._user_name = guest
                self._is_guest = True
                logger.info(
                    "Session identity: no face after hunt — guest %s session=%s",
                    guest,
                    session_id,
                )
                return SessionOpenResult(
                    reply=NO_FACE_GUEST_REPLY,
                    reply_path="session_greet",
                    user_name=guest,
                    is_guest=True,
                )
            self._state = "offer_register"
            self._user_name = None
            logger.info(
                "Session identity: register-offer state=%s session=%s",
                identity_state,
                session_id,
            )
            return SessionOpenResult(
                reply=OFFER_REGISTER_PROMPT,
                reply_path="session_register_offer",
            )

    def end_session(self) -> None:
        with self._lock:
            self._active = False
            self._state = "idle"
            self._clear_pending_spelling()
            self._session_id = ""
            self._opening_echo_budget = 0
            self._declined_register = False

    def should_skip_prompt_echo(self, user_text: str) -> bool:
        """Drop GREET / hello TTS that the Aux-in mics picked up after session open."""
        text = str(user_text or "").strip()
        if not text:
            return False
        with self._lock:
            if not self._active:
                return False
            budget = self._opening_echo_budget
        if is_face_reg_prompt_echo(text):
            return True
        if is_unconditional_greet_echo(text):
            return True
        if budget <= 0:
            return False
        if is_opening_greeting_echo(text):
            with self._lock:
                if self._opening_echo_budget > 0:
                    self._opening_echo_budget -= 1
            logger.info("Session identity: skip opening greet echo heard=%s", text[:80])
            return True
        with self._lock:
            self._opening_echo_budget = 0
        return False

    def handle_voice(self, user_text: str) -> FaceRegVoiceResult:
        text = str(user_text or "").strip()
        with self._lock:
            if not self._active:
                return FaceRegVoiceResult(handled=False)
            state = self._state

        if is_face_reg_prompt_echo(text):
            return FaceRegVoiceResult(handled=True, reply="", relisten_after_reply=True)

        if state == "idle":
            return FaceRegVoiceResult(handled=False)

        if state == "identified":
            denied, wrong_name = parse_misidentification_denial(text)
            if denied:
                return self._handle_misidentification(wrong_name)
            return FaceRegVoiceResult(handled=False)

        if state == "guest":
            return FaceRegVoiceResult(handled=False)

        if is_already_registered_claim(text):
            return self._rerecognize_known_user()

        # "cancel" / "stop the process" → guest and keep talking. Goodbye still ends the session.
        if is_registration_stop_process(text):
            return self._become_guest(declined_register=True)
        if is_session_goodbye_utterance(text):
            return FaceRegVoiceResult(handled=False)

        if state == "offer_register":
            return self._handle_offer(text)
        if state == "awaiting_spell":
            return self._handle_spell_letter(text)
        if state == "awaiting_letter_confirm":
            return self._handle_letter_confirm(text)
        if state == "awaiting_confirm":
            return self._handle_confirm(text)
        return FaceRegVoiceResult(handled=False)

    def _become_identified(self, name: str, *, reply: str) -> FaceRegVoiceResult:
        cleaned = (name or "").strip()
        with self._lock:
            self._state = "identified"
            self._user_name = cleaned
            self._is_guest = False
            self._clear_pending_spelling()
            self._declined_register = False
        logger.info("Session identity: identified name=%s", cleaned)
        return FaceRegVoiceResult(handled=True, reply=reply, registered_name=cleaned)

    def _become_guest(
        self,
        *,
        declined_register: bool = True,
        spoken: bool = True,
        reply: str | None = None,
    ) -> FaceRegVoiceResult:
        guest = new_guest_name()
        with self._lock:
            self._state = "guest"
            self._user_name = guest
            self._is_guest = True
            self._clear_pending_spelling()
            self._declined_register = declined_register
        logger.info(
            "Session identity: guest %s declined_register=%s", guest, declined_register
        )
        spoken_reply = GUEST_REPLY if spoken else (reply or "")
        return FaceRegVoiceResult(
            handled=True, reply=spoken_reply, registered_name=guest
        )

    def timeout_to_guest(self) -> FaceRegVoiceResult:
        """No activity during register: guest, keep STREAM."""
        if not self.in_registration():
            return FaceRegVoiceResult(handled=False)
        result = self._become_guest(declined_register=True, spoken=False)
        return FaceRegVoiceResult(
            handled=True,
            reply="",
            registered_name=result.registered_name,
        )

    def handle_listen_timeout(self) -> FaceRegVoiceResult:
        """5s after letters → confirm the name. Otherwise 30s → guest."""
        with self._lock:
            if not self._active:
                return FaceRegVoiceResult(handled=False)
            state = self._state
            letters = list(self._pending_letters)
        if state == "awaiting_spell" and letters:
            return self._finish_spelling()
        return self.timeout_to_guest()

    def _handle_misidentification(self, wrong_name: str | None) -> FaceRegVoiceResult:
        """User denied the greeted name — re-scan camera and learn if possible."""
        with self._lock:
            prior = self._user_name
            self._user_name = None
        logger.info(
            "Session identity: misidentification denied wrong=%s prior=%s",
            wrong_name or "(unspecified)",
            prior,
        )

        name: str | None = None
        state = "no_face"
        if hasattr(self._faces, "recognize_identity"):
            try:
                name, state = self._faces.recognize_identity(
                    self._read_frame,
                    allow_session_hint=False,
                    allow_pending=True,
                    device_id=self._device_id,
                )
            except Exception:
                logger.exception("Session identity: misidentification re-scan failed")
                name, state = None, "no_face"

        cleaned = (name or "").strip()
        if state == "recognized" and cleaned and cleaned.lower() not in {
            "unknown",
            "face",
        }:
            if wrong_name and cleaned.lower() == wrong_name.lower():
                with self._lock:
                    self._state = "offer_register"
                    self._clear_pending_spelling()
                return FaceRegVoiceResult(
                    handled=True,
                    reply=MISIDENTIFY_STILL_UNKNOWN,
                )
            try:
                capture_face_samples(
                    self._faces,
                    self._read_frame,
                    cleaned,
                    samples=5,
                    interval_ms=200,
                )
                if hasattr(self._faces, "train"):
                    self._faces.train()
                logger.info(
                    "Session identity: correction learned samples for %s", cleaned
                )
            except Exception:
                logger.exception("Session identity: correction sample capture failed")
            return self._become_identified(
                cleaned,
                reply=f"Sorry about that, {cleaned}. I see you now. How can I help?",
            )

        with self._lock:
            self._state = "offer_register"
            self._clear_pending_spelling()
        return FaceRegVoiceResult(
            handled=True,
            reply=f"{MISIDENTIFY_APOLOGY} {MISIDENTIFY_STILL_UNKNOWN}",
        )

    def _rerecognize_known_user(self) -> FaceRegVoiceResult:
        """User says they are not new — check the camera again before giving up."""
        name: str | None = None
        state = "no_face"
        if hasattr(self._faces, "recognize_identity"):
            try:
                name, state = self._faces.recognize_identity(
                    self._read_frame,
                    allow_session_hint=False,
                    allow_pending=True,
                    device_id=self._device_id,
                )
            except Exception:
                logger.exception("Session identity: re-recognize failed")
                name, state = None, "no_face"
        cleaned = (name or "").strip()
        if state == "recognized" and cleaned and cleaned.lower() not in {
            "unknown",
            "face",
        }:
            logger.info("Session identity: not-new claim matched %s", cleaned)
            return self._become_identified(
                cleaned, reply=greet_recognized_user(cleaned)
            )
        with self._lock:
            self._state = "offer_register"
            self._clear_pending_spelling()
        logger.info("Session identity: not-new claim still unknown")
        return FaceRegVoiceResult(handled=True, reply=STILL_UNKNOWN_REPLY)

    def apply_visible_scene(
        self,
        *,
        visible_names: list[str],
        scene_state: str,
        allow_register: bool = True,
    ) -> SessionOpenResult | None:
        """After a hunt: keep / switch user, offer register, or become guest.

        Returns a spoken prompt when one is needed; None means keep listening.
        Does not interrupt an in-progress registration voice flow.

        When @p allow_register is False (next spoken turn after TTS): switch
        identified users or become guest only — do not steal the question with
        a register offer.
        """
        names = [
            n.strip()
            for n in (visible_names or [])
            if n and str(n).strip().lower() not in {"unknown", "face"}
        ]
        scene = (scene_state or "no_face").strip().lower()

        with self._lock:
            if not self._active or self._state in {
                "offer_register",
                "awaiting_spell",
                "awaiting_letter_confirm",
                "awaiting_confirm",
            }:
                return None
            current = (self._user_name or "").strip()
            state = self._state
            declined = self._declined_register

        def _same_user(candidate: str) -> bool:
            return bool(current) and candidate.lower() == current.lower()

        current_visible = next((n for n in names if _same_user(n)), None)
        other_known = next((n for n in names if not _same_user(n)), None)

        if current_visible:
            return None

        if other_known:
            with self._lock:
                self._state = "identified"
                self._user_name = other_known
                self._is_guest = False
                self._clear_pending_spelling()
                self._declined_register = False
            logger.info(
                "Session identity: switch to visible user %s (was %s)",
                other_known,
                current or "(none)",
            )
            return SessionOpenResult(
                reply="",
                reply_path="session_identity_switch",
                user_name=other_known,
                identified=True,
            )

        if scene == "unknown":
            if not allow_register:
                logger.info(
                    "Session identity: unknown face — skip register offer "
                    "(allow_register=False)"
                )
                return None
            if state == "guest" and declined:
                return None
            with self._lock:
                self._state = "offer_register"
                self._user_name = None
                self._is_guest = False
                self._clear_pending_spelling()
            logger.info("Session identity: unknown face after hunt — register offer")
            return SessionOpenResult(
                reply=OFFER_REGISTER_PROMPT,
                reply_path="session_register_offer",
            )

        if state == "identified":
            guest = new_guest_name()
            with self._lock:
                self._state = "guest"
                self._user_name = guest
                self._is_guest = True
                self._clear_pending_spelling()
            logger.info("Session identity: no person after hunt — guest %s", guest)
            return SessionOpenResult(
                reply="",
                reply_path="session_identity_guest",
                user_name=guest,
                is_guest=True,
            )
        return None

    def _begin_spelling(self) -> FaceRegVoiceResult:
        with self._lock:
            self._clear_pending_spelling()
            self._state = "awaiting_spell"
        return FaceRegVoiceResult(handled=True, reply=ASK_SPELL_PROMPT)

    def _finish_spelling(self) -> FaceRegVoiceResult:
        with self._lock:
            name = self._name_from_letters()
            if not name:
                self._state = "awaiting_spell"
                return FaceRegVoiceResult(handled=True, reply=ASK_SPELL_PROMPT)
            self._pending_name = name
            self._state = "awaiting_confirm"
        return FaceRegVoiceResult(handled=True, reply=confirm_name_prompt(name))

    def _handle_offer(self, text: str) -> FaceRegVoiceResult:
        if is_registration_offer_no(text) or is_registration_cancel(text):
            return self._become_guest(declined_register=True)
        if is_registration_offer_yes(text):
            return self._begin_spelling()
        return FaceRegVoiceResult(handled=True, reply=OFFER_REGISTER_PROMPT)

    def _handle_spell_letter(self, text: str) -> FaceRegVoiceResult:
        if is_registration_cancel(text) or is_registration_offer_no(text):
            return self._become_guest(declined_register=True)
        letter = parse_next_spell_letter(text)
        if letter:
            with self._lock:
                self._pending_letter = letter
                self._state = "awaiting_letter_confirm"
            return FaceRegVoiceResult(handled=True, reply=confirm_letter_prompt(letter))
        spelled = parse_spelled_name(text)
        if spelled:
            with self._lock:
                self._pending_letters = [ch.upper() for ch in spelled if ch.isalpha()]
                self._pending_letter = ""
            return self._finish_spelling()
        return FaceRegVoiceResult(handled=True, reply=SPELL_RETRY_PROMPT)

    def _handle_letter_confirm(self, text: str) -> FaceRegVoiceResult:
        if is_registration_cancel(text):
            return self._become_guest(declined_register=True)
        if is_confirm_yes(text):
            with self._lock:
                letter = self._pending_letter
                if letter:
                    self._pending_letters.append(letter)
                self._pending_letter = ""
                self._state = "awaiting_spell"
            logger.info(
                "Session identity: accepted letter=%s so_far=%s",
                letter,
                "".join(self._pending_letters),
            )
            return FaceRegVoiceResult(handled=True, reply="", relisten_after_reply=True)
        if is_confirm_no(text):
            with self._lock:
                self._pending_letter = ""
                self._state = "awaiting_spell"
            return FaceRegVoiceResult(handled=True, reply=LETTER_AGAIN_PROMPT)
        letter = parse_next_spell_letter(text)
        if letter:
            with self._lock:
                self._pending_letter = letter
                self._state = "awaiting_letter_confirm"
            return FaceRegVoiceResult(handled=True, reply=confirm_letter_prompt(letter))
        with self._lock:
            pending = self._pending_letter
        return FaceRegVoiceResult(handled=True, reply=confirm_letter_prompt(pending))

    def _handle_confirm(self, text: str) -> FaceRegVoiceResult:
        if is_confirm_no(text):
            return self._begin_spelling()
        if not is_confirm_yes(text):
            spelled = parse_spelled_name(text)
            if spelled:
                with self._lock:
                    self._pending_letters = [ch.upper() for ch in spelled if ch.isalpha()]
                    self._pending_name = spelled
                return FaceRegVoiceResult(
                    handled=True, reply=confirm_name_prompt(spelled)
                )
            letter = parse_next_spell_letter(text)
            if letter:
                with self._lock:
                    self._pending_letter = letter
                    self._state = "awaiting_letter_confirm"
                return FaceRegVoiceResult(
                    handled=True, reply=confirm_letter_prompt(letter)
                )
            with self._lock:
                name = self._pending_name or self._name_from_letters()
            return FaceRegVoiceResult(handled=True, reply=confirm_name_prompt(name))
        with self._lock:
            if not self._pending_name:
                self._pending_name = self._name_from_letters()
        return self._save_pending_user()

    def _save_pending_user(self) -> FaceRegVoiceResult:
        with self._lock:
            name = self._pending_name or self._name_from_letters()
        if not name:
            return self._begin_spelling()

        frame = self._read_frame() if self._read_frame else None
        existing_before: str | None = None
        if frame is not None and hasattr(self._faces, "identify_registered_face"):
            existing_before = self._faces.identify_registered_face(frame)
        if frame is not None and hasattr(self._faces, "validate_registration_name"):
            allowed, existing = self._faces.validate_registration_name(frame, name)
            if not allowed and existing:
                with self._lock:
                    self._state = "identified"
                    self._user_name = existing
                    self._is_guest = False
                    self._clear_pending_spelling()
                return FaceRegVoiceResult(
                    handled=True,
                    reply=already_registered_reply(existing),
                    already_registered_as=existing,
                    registered_name=existing,
                )

        logger.info("Session identity: capturing samples for %s", name)
        capture = capture_face_samples(
            self._faces,
            self._read_frame,
            name,
            samples=15,
            interval_ms=150,
        )
        with self._lock:
            self._clear_pending_spelling()
            if capture.saved_samples > 0:
                self._state = "identified"
                self._user_name = name
                self._is_guest = False
            else:
                self._state = "awaiting_spell"

        if capture.saved_samples == 0:
            detail = capture.errors[-1] if capture.errors else "No samples saved"
            if detail.startswith("already_registered_as:"):
                existing = detail.split(":", 1)[1]
                with self._lock:
                    self._state = "identified"
                    self._user_name = existing
                    self._is_guest = False
                return FaceRegVoiceResult(
                    handled=True,
                    reply=already_registered_reply(existing),
                    already_registered_as=existing,
                    registered_name=existing,
                )
            logger.warning("Session identity save failed for %s: %s", name, detail)
            return FaceRegVoiceResult(
                handled=True,
                reply=(
                    f"I heard {name}, but I couldn't capture your face. "
                    "Please look at the camera and spell your name again."
                ),
            )

        refresh = bool(
            existing_before
            and hasattr(self._faces, "same_person")
            and self._faces.same_person(name, existing_before)
        )
        reply = (
            same_person_refresh_reply(name)
            if refresh
            else f"All set, {name}. I've registered you. How can I help you?"
        )
        return FaceRegVoiceResult(
            handled=True,
            reply=reply,
            registered_name=name,
        )


_faces: Any = None
_default_read_frame: Callable[[], Any] | None = None
_frame_getter_for_device: Callable[[str | None], Callable[[], Any]] | None = None
_flows: dict[str, SessionIdentityFlow] = {}
_flow_lock = threading.Lock()


def configure_session_identity(
    faces: Any,
    read_frame: Callable[[], Any],
    frame_getter_for_device: Callable[[str | None], Callable[[], Any]] | None = None,
) -> SessionIdentityFlow:
    global _faces, _default_read_frame, _frame_getter_for_device
    with _flow_lock:
        _faces = faces
        _default_read_frame = read_frame
        _frame_getter_for_device = frame_getter_for_device
        _flows.clear()
        default = SessionIdentityFlow(faces, read_frame)
        _flows[""] = default
        return default


def get_session_identity(device_id: str | None = None) -> SessionIdentityFlow | None:
    """Return the identity flow for this robot. Each MAC has its own greet/register state."""
    if _faces is None:
        return None
    key = str(device_id or "").strip()
    with _flow_lock:
        found = _flows.get(key)
        if found is not None:
            return found
        if key and _frame_getter_for_device is not None:
            read_frame = _frame_getter_for_device(key)
        else:
            read_frame = _default_read_frame or (lambda: None)
        found = SessionIdentityFlow(_faces, read_frame)
        _flows[key] = found
        return found
