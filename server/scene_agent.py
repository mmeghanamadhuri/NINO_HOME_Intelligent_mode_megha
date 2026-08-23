"""Per-device spatial agent on top of Qwen-VL, faces, objects, and emotion."""

from __future__ import annotations

import logging
import os
import re
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from session_emotion import looking_phrase, normalize_emotion


def _phrase_visible_scene(names, detections, emotions=None) -> str:
    try:
        from object_detection_service import phrase_visible_scene

        return phrase_visible_scene(names, detections, emotions)
    except Exception:
        people = ", ".join(n for n in (names or []) if n)
        labels = [
            str(d.get("label") or "").strip()
            for d in (detections or [])
            if str(d.get("label") or "").strip()
        ]
        objects = ", ".join(labels)
        if people and objects:
            return f"{people} and {objects}"
        return people or objects


def _spatial_scene_context(detections, *, scene_summary: str = "", names=None) -> str:
    try:
        from object_detection_service import spatial_scene_context

        return spatial_scene_context(
            detections, scene_summary=scene_summary, names=names
        )
    except Exception:
        visible = _phrase_visible_scene(names, detections)
        parts = [p for p in (scene_summary, visible) if p]
        return ". ".join(parts)

logger = logging.getLogger(__name__)

OPENING_LOOK_LINE = "Let me see where you are."
OBSERVE_ACK = (
    "Okay. I'll watch quietly until you say okay Nino, now you can join in."
)
HOW_CAN_I_HELP = "How can I help you?"
SPATIAL_REPORT_MAX_CHARS = int(os.environ.get("SPATIAL_REPORT_MAX_CHARS", "220"))


def trim_spatial_report(text: str, *, max_chars: int | None = None) -> str:
    """Keep wake/spatial TTS short enough for real-time voice."""
    limit = max(40, max_chars if max_chars is not None else SPATIAL_REPORT_MAX_CHARS)
    cleaned = " ".join(str(text or "").split()).strip()
    if len(cleaned) <= limit:
        return cleaned
    chunk = cleaned[:limit]
    for sep in (". ", "? ", "! "):
        idx = chunk.rfind(sep)
        if idx >= 48:
            return chunk[: idx + len(sep)].strip()
    trimmed = chunk.rstrip(" ,;:-")
    if trimmed and trimmed[-1] not in ".!?":
        if len(trimmed) + 1 <= limit:
            trimmed += "."
        else:
            trimmed = trimmed[: limit - 1].rstrip(" ,;:-") + "."
    return trimmed[:limit].strip()

_REGISTER_RE = re.compile(
    r"\b(?:"
    r"register\s+me|"
    r"initiate(?:\s+the)?\s+registration(?:\s+process)?|"
    r"start(?:\s+the)?\s+registration(?:\s+process)?|"
    r"(?:please\s+)?(?:can\s+you|could\s+you|would\s+you)\s+register\s+me|"
    r"i\s+want\s+to\s+register|"
    r"sign\s+me\s+up"
    r")\b",
    re.IGNORECASE,
)

_OBSERVE_RE = re.compile(
    r"\b(?:"
    r"you\s+can\s+observe(?:\s+and\s+help)?|"
    r"observe\s+and\s+help|"
    r"start\s+observing|"
    r"just\s+observe|"
    r"observe\s+quietly|"
    r"keep\s+observing|"
    r"watch\s+quietly(?:\s+and\s+help)?|"
    r"watch\s+and\s+help"
    r")\b",
    re.IGNORECASE,
)

_OK_NINO_RE = re.compile(
    r"^\s*ok(?:ay)?\s+nino\b",
    re.IGNORECASE,
)
_OK_NINO_JOIN_RE = re.compile(
    r"\b(?:"
    r"join(?:\s+in)?|"
    r"explain(?:\s+the\s+context)?|"
    r"what\s+were\s+we\s+talking|"
    r"what\s+was\s+(?:that|going\s+on)|"
    r"context|"
    r"recap|"
    r"tell\s+me|"
    r"what(?:'s| is)\s+going\s+on"
    r")\b",
    re.IGNORECASE,
)


def is_register_request(user_text: str) -> bool:
    text = str(user_text or "").strip()
    return bool(text) and bool(_REGISTER_RE.search(text))


def is_observe_command(user_text: str) -> bool:
    text = str(user_text or "").strip()
    if not text or _OK_NINO_RE.match(text):
        return False
    return bool(_OBSERVE_RE.search(text))


def is_ok_nino_join(user_text: str, *, wake_stripped: bool = False) -> bool:
    """True when the user addresses Nino to leave observe / dump context.

    After wake-word stripping, ``user_text`` is only the command. Pass
    ``wake_stripped=True`` so "now you can join in" still counts.
    """
    text = str(user_text or "").strip()
    if not text:
        return False
    if _OK_NINO_RE.match(text):
        rest = _OK_NINO_RE.sub("", text, count=1).strip(" ,.-")
        if not rest:
            return False
        return bool(_OK_NINO_JOIN_RE.search(rest))
    if wake_stripped:
        return bool(_OK_NINO_JOIN_RE.search(text))
    return False


def _where_phrase(side: str, tilt: str = "center") -> str:
    side_key = str(side or "front").strip().lower()
    tilt_key = str(tilt or "center").strip().lower()
    if side_key == "left":
        base = "on my left"
    elif side_key == "right":
        base = "on my right"
    else:
        base = "in front of me"
    if tilt_key == "up":
        return f"{base}, a bit above eye level"
    if tilt_key == "down":
        return f"{base}, a bit below eye level"
    return base


@dataclass
class SceneSnapshot:
    side: str
    tilt: str
    names: list[str] = field(default_factory=list)
    detections: list[dict[str, Any]] = field(default_factory=list)
    emotions: dict[str, str] = field(default_factory=dict)
    scene_summary: str = ""

    def note_line(self) -> str:
        where = _where_phrase(self.side, self.tilt)
        visible = _phrase_visible_scene(self.names, self.detections) or "nothing distinctive"
        bits = [f"Looking {where}: {visible}"]
        looks = []
        for name, emo in self.emotions.items():
            phrase = looking_phrase(emo)
            if name and phrase:
                looks.append(f"{name} looks {phrase}")
        if looks:
            bits.append("; ".join(looks))
        if self.scene_summary:
            bits.append(self.scene_summary.rstrip("."))
        return ". ".join(bits)


@dataclass(frozen=True)
class SceneState:
    """Merged view of one completed spatial sweep."""

    people: tuple[str, ...] = ()
    objects: tuple[str, ...] = ()
    emotions: tuple[tuple[str, str], ...] = ()
    user_side: str = "front"
    user_tilt: str = "center"
    summaries: tuple[str, ...] = ()

    def people_set(self) -> set[str]:
        return {p.strip().lower() for p in self.people if p.strip()}

    def objects_set(self) -> set[str]:
        return {o.strip().lower() for o in self.objects if o.strip()}

    def emotion_map(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for name, emo in self.emotions:
            key = name.strip().lower()
            if key:
                out[key] = emo
        return out

    def summary_line(self) -> str:
        bits: list[str] = []
        if self.people:
            bits.append("People: " + ", ".join(self.people))
        if self.objects:
            bits.append("Objects: " + ", ".join(self.objects))
        bits.append(f"Primary person at {_where_phrase(self.user_side, self.user_tilt)}")
        if self.summaries:
            bits.append(self.summaries[-1])
        return ". ".join(bits)


@dataclass
class SceneDelta:
    new_people: list[str] = field(default_factory=list)
    gone_people: list[str] = field(default_factory=list)
    new_objects: list[str] = field(default_factory=list)
    gone_objects: list[str] = field(default_factory=list)
    moved: bool = False
    new_side: str = "front"
    new_tilt: str = "center"
    emotion_changes: list[tuple[str, str, str]] = field(default_factory=list)

    def has_changes(self) -> bool:
        return bool(
            self.new_people
            or self.gone_people
            or self.new_objects
            or self.gone_objects
            or self.moved
            or self.emotion_changes
        )


def _object_labels(detections: list[dict[str, Any]]) -> list[str]:
    labels: list[str] = []
    seen: set[str] = set()
    for det in detections or []:
        label = str(det.get("label") or "").strip().lower()
        if not label or label in {"person", "people"} or label in seen:
            continue
        seen.add(label)
        labels.append(label)
    return labels


def merge_snapshots(
    snapshots: list[SceneSnapshot],
    *,
    user_side: str = "front",
    user_tilt: str = "center",
) -> SceneState:
    people: list[str] = []
    seen_people: set[str] = set()
    objects: list[str] = []
    seen_objects: set[str] = set()
    emotions: dict[str, str] = {}
    summaries: list[str] = []
    seen_summaries: set[str] = set()
    for snap in snapshots:
        for name in snap.names:
            key = name.strip().lower()
            if not key or key in seen_people:
                continue
            seen_people.add(key)
            people.append(name.strip())
        for label in _object_labels(snap.detections):
            if label not in seen_objects:
                seen_objects.add(label)
                objects.append(label)
        for name, emo in snap.emotions.items():
            key = name.strip().lower()
            if key:
                emotions[key] = str(emo).strip()
        summary = str(snap.scene_summary or "").strip()
        if summary and summary not in seen_summaries:
            seen_summaries.add(summary)
            summaries.append(summary.rstrip("."))
    emotion_items = tuple(
        (name, emotions[name.strip().lower()])
        for name in people
        if emotions.get(name.strip().lower())
    )
    return SceneState(
        people=tuple(people),
        objects=tuple(objects),
        emotions=emotion_items,
        user_side=str(user_side or "front").strip().lower() or "front",
        user_tilt=str(user_tilt or "center").strip().lower() or "center",
        summaries=tuple(summaries),
    )


def diff_scene_states(previous: SceneState, current: SceneState) -> SceneDelta:
    prev_people = previous.people_set()
    curr_people = current.people_set()
    prev_objects = previous.objects_set()
    curr_objects = current.objects_set()
    prev_display = {p.strip().lower(): p.strip() for p in previous.people}
    curr_display = {p.strip().lower(): p.strip() for p in current.people}
    prev_emotions = previous.emotion_map()
    curr_emotions = current.emotion_map()
    emotion_changes: list[tuple[str, str, str]] = []
    for key in sorted(curr_people & prev_people):
        old = normalize_emotion(prev_emotions.get(key, ""))
        new = normalize_emotion(curr_emotions.get(key, ""))
        if old and new and old != new:
            emotion_changes.append((curr_display.get(key, key), old, new))
    return SceneDelta(
        new_people=[curr_display[k] for k in sorted(curr_people - prev_people)],
        gone_people=[prev_display[k] for k in sorted(prev_people - curr_people)],
        new_objects=[o for o in current.objects if o.lower() in (curr_objects - prev_objects)],
        gone_objects=[o for o in previous.objects if o.lower() in (prev_objects - curr_objects)],
        moved=(previous.user_side, previous.user_tilt)
        != (current.user_side, current.user_tilt),
        new_side=current.user_side,
        new_tilt=current.user_tilt,
        emotion_changes=emotion_changes,
    )


class SceneAgent:
    """Running spatial + conversation context for one robot."""

    def __init__(self, device_id: str = "") -> None:
        self.device_id = str(device_id or "").strip()
        self._lock = threading.Lock()
        self._user_name: str | None = None
        self._is_guest = True
        self._observing = False
        self._snapshots: deque[SceneSnapshot] = deque(maxlen=8)
        self._audio_notes: deque[str] = deque(maxlen=10)
        self._reports: deque[str] = deque(maxlen=6)
        self._user_side = "front"
        self._user_tilt = "center"
        self._voice_name: str | None = None
        self._face_name: str | None = None
        self._identity_source: str = ""
        self._voice_score: float = 0.0
        self._mismatch = False
        self._looking_for: str | None = None
        self._mismatch_note: str | None = None
        self._baseline: SceneState | None = None

    def begin_sweep(self) -> None:
        """Start a fresh pose buffer for the next spatial sweep."""
        with self._lock:
            self._snapshots.clear()

    def _merge_current_sweep_locked(self) -> SceneState:
        return merge_snapshots(
            list(self._snapshots),
            user_side=self._user_side,
            user_tilt=self._user_tilt,
        )

    def _commit_baseline(self, state: SceneState) -> None:
        with self._lock:
            self._baseline = state

    def has_baseline(self) -> bool:
        with self._lock:
            return self._baseline is not None

    def begin_session(self, user_name: str | None, *, is_guest: bool) -> None:
        with self._lock:
            self._user_name = (user_name or "").strip() or None
            self._is_guest = bool(is_guest)
            self._observing = False
            self._snapshots.clear()
            self._audio_notes.clear()
            self._baseline = None
            self._user_side = "front"
            self._user_tilt = "center"
            self._voice_name = None
            self._face_name = None
            self._identity_source = ""
            self._voice_score = 0.0
            self._mismatch = False
            self._looking_for = None
            self._mismatch_note = None

    def end_session(self) -> None:
        with self._lock:
            self._observing = False
            self._user_name = None
            self._is_guest = True
            self._snapshots.clear()
            self._audio_notes.clear()
            self._baseline = None
            self._voice_name = None
            self._face_name = None
            self._identity_source = ""
            self._voice_score = 0.0
            self._mismatch = False
            self._looking_for = None
            self._mismatch_note = None

    def set_user(self, user_name: str | None, *, is_guest: bool) -> None:
        with self._lock:
            self._user_name = (user_name or "").strip() or None
            self._is_guest = bool(is_guest)

    def is_observing(self) -> bool:
        with self._lock:
            return self._observing

    def enter_observe(self) -> None:
        with self._lock:
            self._observing = True

    def exit_observe(self) -> None:
        with self._lock:
            self._observing = False

    def ingest_pose(
        self,
        *,
        side: str,
        tilt: str = "center",
        names: list[str] | None = None,
        detections: list[dict[str, Any]] | None = None,
        emotions: dict[str, str] | None = None,
        scene_summary: str = "",
    ) -> SceneSnapshot:
        snap = SceneSnapshot(
            side=str(side or "front").strip().lower() or "front",
            tilt=str(tilt or "center").strip().lower() or "center",
            names=[n for n in (names or []) if str(n).strip()],
            detections=list(detections or []),
            emotions={
                str(k).strip(): str(v).strip()
                for k, v in (emotions or {}).items()
                if str(k).strip() and str(v).strip()
            },
            scene_summary=str(scene_summary or "").strip(),
        )
        with self._lock:
            self._snapshots.append(snap)
            user = (self._user_name or "").strip().lower()
            looking = (self._looking_for or user or "").strip().lower()
            if looking and any(n.strip().lower() == looking for n in snap.names):
                self._user_side = snap.side
                self._user_tilt = snap.tilt
                if self._looking_for and looking == self._looking_for.strip().lower():
                    self._looking_for = None
            elif user and any(n.strip().lower() == user for n in snap.names):
                self._user_side = snap.side
                self._user_tilt = snap.tilt
            elif not user and snap.names:
                self._user_side = snap.side
                self._user_tilt = snap.tilt
        logger.info(
            "Scene agent ingest side=%s tilt=%s names=%s objects=%s device=%s",
            snap.side,
            snap.tilt,
            snap.names,
            [d.get("label") for d in snap.detections[:8]],
            self.device_id or "-",
        )
        return snap

    def note_speaker(
        self,
        *,
        viewer_name: str | None = None,
        voice_name: str | None = None,
        face_name: str | None = None,
        source: str = "",
        score: float = 0.0,
        mismatch: bool = False,
        mismatch_note: str | None = None,
        looking_for: bool = False,
    ) -> None:
        """Record who the voice profile and camera say is speaking."""
        voice = (voice_name or "").strip() or None
        face = (face_name or "").strip() or None
        viewer = (viewer_name or "").strip() or None
        with self._lock:
            self._voice_name = voice
            self._face_name = face
            self._identity_source = str(source or "").strip()
            self._voice_score = float(score or 0.0)
            self._mismatch = bool(mismatch)
            self._mismatch_note = (mismatch_note or "").strip() or None
            if looking_for and voice:
                self._looking_for = voice
            elif looking_for and viewer and not str(viewer).lower().startswith("guest"):
                self._looking_for = viewer
            if viewer and not str(viewer).lower().startswith("guest"):
                self._user_name = viewer
                self._is_guest = False

    def note_audio(self, text: str, speaker: str | None = None) -> None:
        cleaned = str(text or "").strip()
        if not cleaned:
            return
        who = (speaker or "").strip()
        line = f"{who}: {cleaned}" if who and not who.lower().startswith("guest") else cleaned
        with self._lock:
            self._audio_notes.append(line)

    def remember_report(self, report: str) -> None:
        cleaned = str(report or "").strip()
        if not cleaned:
            return
        with self._lock:
            self._reports.append(cleaned)

    def _speaker_line_locked(self) -> str:
        parts: list[str] = []
        if self._voice_name:
            score = f" ({self._voice_score:.2f})" if self._voice_score else ""
            parts.append(f"Voice sounds like {self._voice_name}{score}")
        if self._face_name:
            parts.append(f"camera last saw {self._face_name}")
        if self._mismatch and self._mismatch_note:
            parts.append(self._mismatch_note)
        elif self._mismatch and self._voice_name and self._face_name:
            parts.append(f"I see {self._face_name} but I hear {self._voice_name}")
        if self._looking_for:
            parts.append(f"looking for {self._looking_for}")
        if self._identity_source:
            parts.append(f"source={self._identity_source}")
        return ". ".join(parts)

    def format_notes(self) -> str:
        with self._lock:
            snaps = list(self._snapshots)
            audio = list(self._audio_notes)
            reports = list(self._reports)
            user = self._user_name
            guest = self._is_guest
            side = self._user_side
            tilt = self._user_tilt
            speaker = self._speaker_line_locked()
        lines: list[str] = []
        who = "a guest" if guest or not user else user
        lines.append(f"Primary person: {who}, last seen {_where_phrase(side, tilt)}.")
        if speaker:
            lines.append(f"Speaker: {speaker}.")
        if snaps:
            lines.append("Sweep notes:")
            lines.extend(f"- {snap.note_line()}" for snap in snaps)
        if audio:
            lines.append("Heard while watching:")
            lines.extend(f"- {note}" for note in audio)
        if reports:
            lines.append("Earlier spatial reports:")
            lines.extend(f"- {item}" for item in reports)
        with self._lock:
            baseline = self._baseline
        if baseline is not None:
            lines.append("Remembered room state:")
            lines.append(f"- {baseline.summary_line()}")
        return "\n".join(lines)

    def context_block(self) -> str:
        with self._lock:
            snaps = list(self._snapshots)
            reports = list(self._reports)
            audio = list(self._audio_notes)
            user = self._user_name
            guest = self._is_guest
            side = self._user_side
            tilt = self._user_tilt
            speaker = self._speaker_line_locked()
            baseline = self._baseline
        parts: list[str] = []
        who = "a guest" if guest or not user else user
        parts.append(f"{who} is {_where_phrase(side, tilt)}")
        if speaker:
            parts.append(speaker)
        if baseline is not None:
            parts.append("Room memory: " + baseline.summary_line())
        if snaps:
            last = snaps[-1]
            scene = _spatial_scene_context(
                last.detections,
                scene_summary=last.scene_summary,
                names=last.names,
            )
            if scene:
                parts.append(scene)
            people: list[str] = []
            seen: set[str] = set()
            for snap in snaps:
                for name in snap.names:
                    key = name.strip().lower()
                    if key and key not in seen:
                        seen.add(key)
                        look = looking_phrase(snap.emotions.get(name))
                        people.append(f"{name} looks {look}" if look else name)
            if people:
                parts.append("People seen: " + ", ".join(people))
        if audio:
            parts.append("Recently heard: " + " | ".join(audio[-4:]))
        if reports:
            parts.append("Prior look: " + reports[-1])
        return ". ".join(p.rstrip(".") for p in parts if p).strip()

    def dominant_emotion(self) -> str:
        with self._lock:
            user = (self._user_name or "").strip().lower()
            snaps = list(self._snapshots)
        for snap in reversed(snaps):
            if user:
                for name, emo in snap.emotions.items():
                    if name.strip().lower() == user:
                        key = normalize_emotion(emo)
                        if key:
                            return key
            for emo in snap.emotions.values():
                key = normalize_emotion(emo)
                if key:
                    return key
        return ""

    def user_last_side(self) -> str:
        with self._lock:
            return self._user_side

    def suggest_motion(self) -> list[str]:
        with self._lock:
            looking = bool(self._looking_for or self._mismatch)
            side = self._user_side
        if looking:
            return ["look_left", "look_right"]
        if side == "left":
            return ["look_left"]
        if side == "right":
            return ["look_right"]
        return ["nod"]

    def suggest_eye(self, *, reply_path: str = "") -> str | None:
        path = str(reply_path or "").strip().lower()
        if path in {"observe", "observe_ack"}:
            return "curious"
        with self._lock:
            mismatch = self._mismatch
            looking = bool(self._looking_for)
            user = self._user_name
            guest = self._is_guest
        if mismatch or looking:
            return "surprised" if mismatch else "curious"
        if path in {"session_greet"}:
            return "heart" if user and not guest else "curious"
        mood = self.dominant_emotion()
        from eye_expression import emotion_eye_from_context, spatial_eye_from_text

        spatial = spatial_eye_from_text(self.context_block())
        if spatial:
            return spatial
        mood_eye = emotion_eye_from_context(mood)
        if mood_eye:
            return mood_eye
        if path in {"spatial_report", "observe_briefing", "look_scan", "look_scan_llm"}:
            return "curious"
        return None

    def _fallback_report(self, *, ask_help: bool) -> str:
        with self._lock:
            snaps = list(self._snapshots)
            user = self._user_name
            guest = self._is_guest
            side = self._user_side
            tilt = self._user_tilt
            mismatch_note = self._mismatch_note
            voice = self._voice_name
            face = self._face_name
            looking = self._looking_for
        who = "" if guest or not user else user
        where = _where_phrase(side, tilt)
        sentences: list[str] = []
        if mismatch_note:
            sentences.append(mismatch_note.rstrip(".") + ".")
        elif voice and face and voice.lower() != face.lower():
            sentences.append(f"I hear {voice} but I see {face}.")
        elif looking:
            sentences.append(f"I hear {looking}. Let me find you.")
        if who:
            sentences.append(f"You're {where}.")
        else:
            sentences.append(f"You're {where} — I'll keep this as a guest chat.")
        people: list[str] = []
        objects: list[str] = []
        seen_people: set[str] = set()
        seen_objects: set[str] = set()
        summaries: list[str] = []
        for snap in snaps:
            for name in snap.names:
                key = name.strip().lower()
                if not key or key in seen_people:
                    continue
                seen_people.add(key)
                look = looking_phrase(snap.emotions.get(name))
                people.append(f"{name}, who looks {look}" if look else name)
            for det in snap.detections:
                label = str(det.get("label") or "").strip().lower()
                if not label or label in seen_objects or label in {"person", "people"}:
                    continue
                seen_objects.add(label)
                objects.append(label)
            if snap.scene_summary and snap.scene_summary not in summaries:
                summaries.append(snap.scene_summary.rstrip("."))
        if people:
            sentences.append("I also see " + ", ".join(people) + ".")
        elif who:
            sentences.append("I don't see anyone else nearby.")
        if objects:
            sentences.append("Around you there is " + ", ".join(objects[:8]) + ".")
        if summaries:
            sentences.append(summaries[-1] + ".")
        if ask_help:
            sentences.append(HOW_CAN_I_HELP)
        report = " ".join(sentences)
        if ask_help and len(report) > SPATIAL_REPORT_MAX_CHARS:
            help_suffix = f" {HOW_CAN_I_HELP}"
            body = trim_spatial_report(
                report[: -len(help_suffix)].strip(),
                max_chars=max(80, SPATIAL_REPORT_MAX_CHARS - len(help_suffix)),
            ).rstrip(".!?")
            report = f"{body}{help_suffix}"
        return trim_spatial_report(report)

    def _fallback_delta_report(self, delta: SceneDelta) -> str:
        sentences: list[str] = []
        if delta.new_people:
            if len(delta.new_people) == 1:
                sentences.append(f"I see {delta.new_people[0]} now.")
            else:
                sentences.append("I see " + ", ".join(delta.new_people) + " now.")
        if delta.gone_people:
            if len(delta.gone_people) == 1:
                sentences.append(f"{delta.gone_people[0]} is no longer in view.")
            else:
                sentences.append(
                    ", ".join(delta.gone_people) + " are no longer in view."
                )
        if delta.new_objects:
            sentences.append(
                "I notice " + ", ".join(delta.new_objects[:4]) + " now."
            )
        if delta.gone_objects:
            sentences.append(
                "I no longer see " + ", ".join(delta.gone_objects[:4]) + "."
            )
        if delta.moved:
            where = _where_phrase(delta.new_side, delta.new_tilt)
            sentences.append(f"You're {where} now.")
        for name, _old, new in delta.emotion_changes:
            phrase = looking_phrase(new)
            if phrase:
                sentences.append(f"{name} looks {phrase} now.")
        return trim_spatial_report(" ".join(sentences))

    def _compose_full_report(self, *, ask_help: bool, mode: str) -> str:
        notes = self.format_notes()
        fallback = self._fallback_report(ask_help=ask_help)
        if not notes.strip():
            return fallback
        prompt = (
            "You are NiNO, a home robot that just looked around the room.\n"
            f"Mode: {mode}.\n"
            f"{notes}\n"
            "Speak 1 or 2 short spoken sentences only. Mention who you see, "
            "where they are, and one notable object if relevant. Warm and natural. "
            "No lists, no markdown, no stage directions."
        )
        if ask_help:
            prompt += f' End by asking "{HOW_CAN_I_HELP}"'
        try:
            from llm_service import ollama_chat

            text = ollama_chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "You are NiNO. Use only the sweep notes. "
                            "If voice and camera disagree, trust the speaker and say you "
                            "are looking for them. Never invent people or objects that "
                            "are not listed. Keep replies under 220 characters."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                timeout_s=25,
                num_predict=64,
                temperature=0.4,
            )
            cleaned = trim_spatial_report(str(text or "").strip())
            if not cleaned or cleaned.startswith("Sorry, I could not reach"):
                return fallback
            if ask_help and "how can i help" not in cleaned.lower():
                cleaned = trim_spatial_report(
                    f"{cleaned.rstrip('.!?')} {HOW_CAN_I_HELP}"
                )
            return cleaned
        except Exception:
            logger.warning(
                "Scene agent compose failed device=%s",
                self.device_id or "-",
                exc_info=True,
            )
            return fallback

    def _compose_delta_report(self, delta: SceneDelta, *, mode: str) -> str:
        fallback = self._fallback_delta_report(delta)
        with self._lock:
            baseline = self._baseline
        if baseline is None:
            return fallback
        current = self._merge_current_sweep_locked()
        prompt = (
            "You are NiNO, a home robot that just looked around the room again.\n"
            f"Mode: {mode}.\n"
            f"Previous room state:\n- {baseline.summary_line()}\n"
            f"Current room state:\n- {current.summary_line()}\n"
            "Changes detected:\n"
            f"- New people: {', '.join(delta.new_people) or 'none'}\n"
            f"- People gone: {', '.join(delta.gone_people) or 'none'}\n"
            f"- New objects: {', '.join(delta.new_objects) or 'none'}\n"
            f"- Objects gone: {', '.join(delta.gone_objects) or 'none'}\n"
            f"- Primary person moved: {'yes' if delta.moved else 'no'}\n"
            f"- Emotion changes: {delta.emotion_changes or 'none'}\n"
            "Speak 1 short sentence about ONLY what changed. Do not repeat unchanged "
            "details. No greeting, no question, no markdown."
        )
        try:
            from llm_service import ollama_chat

            text = ollama_chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "You are NiNO. Mention only the listed changes. "
                            "Never invent people or objects. Keep replies under 160 characters."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                timeout_s=20,
                num_predict=48,
                temperature=0.35,
            )
            cleaned = trim_spatial_report(str(text or "").strip(), max_chars=160)
            if not cleaned or cleaned.startswith("Sorry, I could not reach"):
                return fallback
            return cleaned
        except Exception:
            logger.warning(
                "Scene agent delta compose failed device=%s",
                self.device_id or "-",
                exc_info=True,
            )
            return fallback

    def compose_report(self, *, ask_help: bool = True, mode: str = "opening") -> str:
        with self._lock:
            baseline = self._baseline
            current = self._merge_current_sweep_locked()

        if mode == "observe_join" or baseline is None:
            report = self._compose_full_report(ask_help=ask_help, mode=mode)
            if report.strip():
                self._commit_baseline(current)
            return report

        delta = diff_scene_states(baseline, current)
        if not delta.has_changes():
            self._commit_baseline(current)
            logger.info(
                "Scene agent sweep unchanged device=%s people=%s objects=%s",
                self.device_id or "-",
                list(current.people),
                list(current.objects),
            )
            return ""

        report = self._compose_delta_report(delta, mode=mode)
        self._commit_baseline(current)
        return report

    def persist_report(
        self,
        report: str,
        *,
        user_text: str,
        reply_path: str,
        session_id: str = "",
    ) -> None:
        self.remember_report(report)
        name = None
        with self._lock:
            if self._user_name and not self._is_guest:
                name = self._user_name
        if not name or not report.strip():
            return
        try:
            from memory_service import get_memory_service

            get_memory_service().log_conversation_for_viewer(
                name,
                user_text,
                report,
                reply_path=reply_path,
                session_id=session_id,
            )
        except Exception:
            logger.exception("Scene agent persist failed user=%s", name)


_agents: dict[str, SceneAgent] = {}
_agent_lock = threading.Lock()


def get_scene_agent(device_id: str | None = None) -> SceneAgent:
    key = str(device_id or "").strip()
    with _agent_lock:
        found = _agents.get(key)
        if found is None:
            found = SceneAgent(device_id=key)
            _agents[key] = found
        return found


def reset_scene_agent(device_id: str | None = None) -> None:
    key = str(device_id or "").strip()
    with _agent_lock:
        agent = _agents.pop(key, None)
    if agent is not None:
        agent.end_session()
