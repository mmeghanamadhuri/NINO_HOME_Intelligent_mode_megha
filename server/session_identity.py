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
    extract_registration_name,
    is_confirm_no,
    is_confirm_yes,
    is_face_reg_prompt_echo,
    is_incomplete_name_phrase,
    is_registration_cancel,
    is_registration_offer_no,
    is_registration_offer_yes,
    is_session_end_utterance,
    parse_spelled_name,
    spell_name_aloud,
)
from stream_asr import DEFAULT_REGISTER_MAX_MS

logger = logging.getLogger(__name__)

SessionIdentState = Literal[
    "idle",
    "offer_register",
    "awaiting_name",
    "awaiting_spell",
    "awaiting_confirm",
    "guest",
    "identified",
]

OFFER_REGISTER_PROMPT = "Looks like you are a new user, can I register you"
ASK_NAME_PROMPT = "What should I call you?"
ASK_SPELL_PROMPT = "Please spell that name for me."
GUEST_REPLY = "No problem. I'll keep this as a guest chat. How can I help you?"
# Silence during register → guest (same as saying "no"). Skip extra TTS.
REGISTER_SILENCE_MS = DEFAULT_REGISTER_MAX_MS
NAME_RETRY_PROMPT = "I didn't catch your name. Please say your name."
SPELL_RETRY_PROMPT = "I didn't catch the spelling. Please spell the name."
CONFIRM_RETRY_PROMPT = "Okay, let's try again. What should I call you?"


def greet_recognized_user(name: str) -> str:
    cleaned = (name or "").strip()
    if not cleaned:
        return "Hey, how can I help you"
    return f"Hey {cleaned}, how can I help you"


def confirm_name_prompt(name: str) -> str:
    letters = spell_name_aloud(name)
    return f"I heard {name}. That's {letters}. Is that right?"


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
        self._user_name: str | None = None
        self._is_guest = False
        self._session_id = ""
        self._device_id = ""
        self._active = False

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
                "awaiting_name",
                "awaiting_spell",
                "awaiting_confirm",
            }

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
            self._pending_name = ""
            self._is_guest = False
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
            self._pending_name = ""
            self._session_id = ""

    def handle_voice(self, user_text: str) -> FaceRegVoiceResult:
        text = str(user_text or "").strip()
        with self._lock:
            if not self._active:
                return FaceRegVoiceResult(handled=False)
            state = self._state

        if state == "idle" or state == "identified" or state == "guest":
            return FaceRegVoiceResult(handled=False)

        if is_face_reg_prompt_echo(text):
            return FaceRegVoiceResult(handled=True, reply="", relisten_after_reply=True)

        # Goodbye / stop still end the session — do not convert to guest.
        if is_session_end_utterance(text):
            return FaceRegVoiceResult(handled=False)

        if state == "offer_register":
            return self._handle_offer(text)
        if state == "awaiting_name":
            return self._handle_name(text)
        if state == "awaiting_spell":
            return self._handle_spell(text)
        if state == "awaiting_confirm":
            return self._handle_confirm(text)
        return FaceRegVoiceResult(handled=False)

    def _become_guest(self) -> FaceRegVoiceResult:
        guest = new_guest_name()
        with self._lock:
            self._state = "guest"
            self._user_name = guest
            self._is_guest = True
            self._pending_name = ""
        logger.info("Session identity: guest %s", guest)
        return FaceRegVoiceResult(handled=True, reply=GUEST_REPLY, registered_name=guest)

    def timeout_to_guest(self) -> FaceRegVoiceResult:
        """60s of no speech during register: same as declining — guest, keep STREAM."""
        if not self.in_registration():
            return FaceRegVoiceResult(handled=False)
        result = self._become_guest()
        # Skip the spoken guest line so listen resumes immediately.
        return FaceRegVoiceResult(
            handled=True,
            reply="",
            registered_name=result.registered_name,
        )

    def _handle_offer(self, text: str) -> FaceRegVoiceResult:
        if is_registration_offer_no(text) or is_registration_cancel(text):
            return self._become_guest()
        if is_registration_offer_yes(text):
            with self._lock:
                self._state = "awaiting_name"
            return FaceRegVoiceResult(handled=True, reply=ASK_NAME_PROMPT)
        # "My name is X" during the offer still counts as yes + name.
        name = extract_registration_name(text)
        if name:
            return self._accept_spoken_name(name)
        return FaceRegVoiceResult(handled=True, reply=OFFER_REGISTER_PROMPT)

    def _handle_name(self, text: str) -> FaceRegVoiceResult:
        if is_registration_cancel(text) or is_registration_offer_no(text):
            return self._become_guest()
        if is_incomplete_name_phrase(text):
            return FaceRegVoiceResult(handled=True, reply=NAME_RETRY_PROMPT)
        name = extract_registration_name(text)
        if not name:
            return FaceRegVoiceResult(handled=True, reply=NAME_RETRY_PROMPT)
        return self._accept_spoken_name(name)

    def _accept_spoken_name(self, name: str) -> FaceRegVoiceResult:
        with self._lock:
            self._pending_name = name
            self._state = "awaiting_spell"
        return FaceRegVoiceResult(
            handled=True,
            reply=f"Thanks. {ASK_SPELL_PROMPT}",
        )

    def _handle_spell(self, text: str) -> FaceRegVoiceResult:
        if is_registration_cancel(text):
            return self._become_guest()
        spelled = parse_spelled_name(text)
        with self._lock:
            pending = self._pending_name
        if spelled:
            # Prefer letters when they match, otherwise keep the spoken name
            # if the spelling is a plausible confirmation of the same word.
            if spelled.lower() == pending.lower() or spelled.lower().replace(" ", "") == pending.lower().replace(" ", ""):
                pending = spelled if len(spelled) >= len(pending) else pending
            elif len(spelled) >= 2:
                pending = spelled
            with self._lock:
                self._pending_name = pending
                self._state = "awaiting_confirm"
            return FaceRegVoiceResult(handled=True, reply=confirm_name_prompt(pending))
        # They may have repeated the name instead of spelling.
        repeated = extract_registration_name(text)
        if repeated:
            with self._lock:
                self._pending_name = repeated
                self._state = "awaiting_confirm"
            return FaceRegVoiceResult(handled=True, reply=confirm_name_prompt(repeated))
        return FaceRegVoiceResult(handled=True, reply=SPELL_RETRY_PROMPT)

    def _handle_confirm(self, text: str) -> FaceRegVoiceResult:
        if is_confirm_no(text):
            with self._lock:
                self._pending_name = ""
                self._state = "awaiting_name"
            return FaceRegVoiceResult(handled=True, reply=CONFIRM_RETRY_PROMPT)
        if not is_confirm_yes(text):
            # Another spelling pass.
            spelled = parse_spelled_name(text)
            if spelled:
                with self._lock:
                    self._pending_name = spelled
                return FaceRegVoiceResult(
                    handled=True, reply=confirm_name_prompt(spelled)
                )
            return FaceRegVoiceResult(
                handled=True,
                reply=confirm_name_prompt(self._pending_name),
            )
        return self._save_pending_user()

    def _save_pending_user(self) -> FaceRegVoiceResult:
        with self._lock:
            name = self._pending_name
        if not name:
            with self._lock:
                self._state = "awaiting_name"
            return FaceRegVoiceResult(handled=True, reply=NAME_RETRY_PROMPT)

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
                    self._pending_name = ""
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
            self._pending_name = ""
            if capture.saved_samples > 0:
                self._state = "identified"
                self._user_name = name
                self._is_guest = False
            else:
                self._state = "awaiting_name"

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
                    "Please look at the camera and say your name again."
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


_flow: SessionIdentityFlow | None = None
_flow_lock = threading.Lock()


def configure_session_identity(faces: Any, read_frame: Callable[[], Any]) -> SessionIdentityFlow:
    global _flow
    with _flow_lock:
        _flow = SessionIdentityFlow(faces, read_frame)
        return _flow


def get_session_identity() -> SessionIdentityFlow | None:
    return _flow
