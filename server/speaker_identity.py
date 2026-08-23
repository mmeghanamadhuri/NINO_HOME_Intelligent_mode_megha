"""Cross-modal face + voice speaker resolution for voice sessions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from voice_profile_service import VoiceMatchState

CameraIdentityState = Literal["recognized", "unknown", "no_face"]
IdentitySource = Literal[
    "face_voice_agree",
    "face",
    "voice",
    "session",
    "face_voice_mismatch_voice",
    "face_voice_mismatch_face",
    "guest",
    "none",
]


@dataclass(frozen=True)
class SpeakerIdentityResult:
    viewer_name: str | None
    memory_name: str | None
    face_name: str | None
    voice_name: str | None
    voice_score: float
    voice_state: VoiceMatchState
    face_state: CameraIdentityState
    source: IdentitySource
    identity_mismatch: bool = False
    trigger_look_scan: bool = False
    mismatch_note: str | None = None


def _clean_name(name: str | None) -> str | None:
    raw = (name or "").strip()
    if not raw or raw.lower() in {"unknown", "face"}:
        return None
    return raw


def _is_guest(name: str | None) -> bool:
    raw = (name or "").strip().lower()
    return raw.startswith("guest-") or raw == "guest"


def _names_agree(a: str | None, b: str | None, *, same_person) -> bool:
    left = _clean_name(a)
    right = _clean_name(b)
    if not left or not right:
        return False
    if left.lower() == right.lower():
        return True
    if same_person is not None:
        try:
            return bool(same_person(left, right))
        except Exception:
            return False
    return False


def resolve_speaker_identity(
    *,
    face_name: str | None,
    face_state: CameraIdentityState,
    voice_name: str | None,
    voice_score: float,
    voice_state: VoiceMatchState,
    session_user: str | None = None,
    session_guest: bool = False,
    visible_names: list[str] | None = None,
    same_person=None,
    voice_high_threshold: float = 0.72,
) -> SpeakerIdentityResult:
    """Merge camera face and voice profile signals into one active speaker."""
    face = _clean_name(face_name)
    voice = _clean_name(voice_name)
    visible = [_clean_name(n) for n in (visible_names or [])]
    visible = [n for n in visible if n]

    voice_confident = voice_state == "recognized" and voice is not None
    voice_soft = voice_state == "unknown" and voice is not None and voice_score >= voice_high_threshold - 0.08
    face_recognized = face_state == "recognized" and face is not None

    agree = _names_agree(face, voice, same_person=same_person)

    if face_recognized and voice_confident and agree:
        return SpeakerIdentityResult(
            viewer_name=face,
            memory_name=face,
            face_name=face,
            voice_name=voice,
            voice_score=voice_score,
            voice_state=voice_state,
            face_state=face_state,
            source="face_voice_agree",
        )

    if face_recognized and voice_confident and not agree:
        # Trust voice when both are confident but disagree; pan to find the speaker.
        note = None
        if face and voice:
            note = f"I see {face} but I hear {voice}."
        return SpeakerIdentityResult(
            viewer_name=voice,
            memory_name=voice,
            face_name=face,
            voice_name=voice,
            voice_score=voice_score,
            voice_state=voice_state,
            face_state=face_state,
            source="face_voice_mismatch_voice",
            identity_mismatch=True,
            trigger_look_scan=True,
            mismatch_note=note,
        )

    if face_recognized:
        return SpeakerIdentityResult(
            viewer_name=face,
            memory_name=face,
            face_name=face,
            voice_name=voice,
            voice_score=voice_score,
            voice_state=voice_state,
            face_state=face_state,
            source="face",
        )

    if voice_confident or voice_soft:
        # No reliable face — trust voice and scan for the speaker.
        target = voice
        if target and visible and not any(
            _names_agree(target, n, same_person=same_person) for n in visible
        ):
            return SpeakerIdentityResult(
                viewer_name=target,
                memory_name=target,
                face_name=face,
                voice_name=voice,
                voice_score=voice_score,
                voice_state=voice_state,
                face_state=face_state,
                source="voice",
                trigger_look_scan=True,
            )
        return SpeakerIdentityResult(
            viewer_name=target,
            memory_name=target,
            face_name=face,
            voice_name=voice,
            voice_score=voice_score,
            voice_state=voice_state,
            face_state=face_state,
            source="voice",
            trigger_look_scan=not visible,
        )

    session = _clean_name(session_user)
    if session and not session_guest:
        return SpeakerIdentityResult(
            viewer_name=session,
            memory_name=session,
            face_name=face,
            voice_name=voice,
            voice_score=voice_score,
            voice_state=voice_state,
            face_state=face_state,
            source="session",
        )

    if session and session_guest:
        return SpeakerIdentityResult(
            viewer_name=session,
            memory_name=None,
            face_name=face,
            voice_name=voice,
            voice_score=voice_score,
            voice_state=voice_state,
            face_state=face_state,
            source="guest",
        )

    return SpeakerIdentityResult(
        viewer_name=None,
        memory_name=None,
        face_name=face,
        voice_name=voice,
        voice_score=voice_score,
        voice_state=voice_state,
        face_state=face_state,
        source="none",
        trigger_look_scan=face_state == "no_face" and bool(visible),
    )
