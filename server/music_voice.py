"""Voice command parsing and spoken replies for robot-speaker music playback."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Leading wake words / politeness that STT keeps in front of the real command.
_PREFIX_RE = re.compile(
    r"^\s*(?:hey\s+|hi\s+|ok(?:ay)?\s+)?(?:nino|neno|nina|ninu)?[\s,]*"
    r"(?:please\s+|can\s+you\s+|could\s+you\s+|would\s+you\s+|i\s+want\s+(?:you\s+to\s+)?)*",
    re.IGNORECASE,
)

# Trailing "... for me / please / now" that is not part of the song name.
_SUFFIX_RE = re.compile(
    r"(?:\s+(?:on|in|from|using|through|with)\s+(?:spotify|youtube|the\s+speaker))?"
    r"(?:\s+(?:for\s+me|please|now|right\s+now))*"
    r"\s*[.!?…]*\s*$",
    re.IGNORECASE,
)

# "play <title>" is unambiguous enough to accept any object; the weaker verbs
# ("put on", "turn on", "start") also drive lights/alarms, so they need a music word.
_STRONG_PLAY_RE = re.compile(
    r"^(?:play|start\s+playing|plai|plea|pley|piyo)\s*(?P<rest>.+)$",
    re.IGNORECASE,
)

_WEAK_PLAY_RE = re.compile(
    r"^(?:put\s+on|turn\s+on|start)\s+(?P<rest>.+)$",
    re.IGNORECASE,
)

# Things you "play" that are not songs — leave these to the LLM.
_NON_MUSIC_OBJECT_RE = re.compile(
    r"^(?:a|an|the|some|my)?\s*"
    r"(?:game|games|joke|jokes|video|videos|movie|movies|film|films|"
    r"football|cricket|chess|quiz|trivia|riddle|riddles|puzzle|"
    r"hide\s+and\s+seek|rock\s+paper\s+scissors|tic\s+tac\s+toe|"
    r"with\s+me|along|dead|it\s+safe|fair)\b",
    re.IGNORECASE,
)

_GENERIC_MUSIC_RE = re.compile(
    r"^(?:a\s+|an\s+|some\s+|any\s+|my\s+|the\s+)?"
    r"(?:music|songs?|tunes?|tracks?|playlist)$",
    re.IGNORECASE,
)

_TITLE_PREFIX_RE = re.compile(
    r"^(?:me\s+|us\s+)?"
    r"(?:the\s+song\s+|the\s+track\s+|a\s+song(?:\s+(?:called|named))?\s*|"
    r"song\s+|track\s+)",
    re.IGNORECASE,
)

_DEFAULT_PLAY_QUERY = "popular songs"

_MUSIC_WORD_RE = re.compile(
    r"\b(?:music|song|songs|track|tracks|tune|tunes|playback|playing)\b",
    re.IGNORECASE,
)

# STT often mashes "shut up" / "stop" into one blob: shupupo, shutut, shuttup.
_SHUTUP_GARBLE_RE = re.compile(
    r"shu(?:t+\s*u+[pt]+|p+\s*u+p*o*|[pt]+u+[pt]+)",
    re.IGNORECASE,
)

# Explicit stop-the-music utterances. Keep this tight so "stop listening" and
# "that's all" still reach goodbye when nothing is playing.
_STOP_RE = re.compile(
    r"^(?:please\s+)?"
    r"(?:pause|stop|halt|silence|(?:be\s+)?quiet|that(?:'s|s| is)\s+enough|enough|cancel|mute|"
    r"shut\s*off|"
    r"shu(?:t+\s*u+[pt]+|p+\s*u+p*o*|[pt]+u+[pt]+)|"
    r"turn\s+(?:it\s+)?off|cut\s+(?:it(?:\s+out)?|off)|kill)"
    r"(?:\s+(?:playing|the\s+playback|it|that|this))?"
    r"(?:\s+(?:the\s+|my\s+|this\s+|that\s+)?(?:music|song|songs|track|tracks|tune|playback))?"
    r"(?:\s+please)?$",
    re.IGNORECASE,
)

# While a track is playing, any of these tokens in a short utterance is a stop.
_STOP_WHILE_PLAYING_RE = re.compile(
    r"\b(?:"
    r"stop|pause|halt|silence|quiet|enough|cancel|mute|"
    r"shut\s*off|turn\s+off|cut\s+it|no\s+more"
    r")\b",
    re.IGNORECASE,
)

_NOW_PLAYING_RE = re.compile(
    r"^(?:what(?:'s| is| song is)?|which\s+song(?:\s+is)?|who\s+(?:sings|is\s+singing))"
    r"[\s\w]*?\b(?:playing|song|track|this)\b[\s\w]*$",
    re.IGNORECASE,
)


@dataclass
class MusicVoiceResult:
    handled: bool
    reply: str = ""
    reply_path: str = "music"


def _normalize(user_text: str) -> str:
    text = str(user_text or "").strip()
    if not text:
        return ""
    text = _PREFIX_RE.sub("", text, count=1)
    text = _SUFFIX_RE.sub("", text, count=1)
    return re.sub(r"\s+", " ", text).strip()


def mentions_music(user_text: str) -> bool:
    return bool(_MUSIC_WORD_RE.search(str(user_text or "")))


def looks_like_stop_while_playing(user_text: str) -> bool:
    """True for stop/shutup phrasing, including STT mashups, once music is on."""
    text = _normalize(user_text)
    if not text:
        return False
    if _STOP_RE.match(text):
        return True
    if _SHUTUP_GARBLE_RE.search(text):
        return True
    if _STOP_WHILE_PLAYING_RE.search(text) and len(text.split()) <= 8:
        return True
    return False


def parse_music_command(user_text: str) -> tuple[str, str] | None:
    """Return (action, query) for a music command, or None if unrelated.

    Actions: play, stop, now_playing.
    """
    text = _normalize(user_text)
    if not text:
        return None

    if _NOW_PLAYING_RE.match(text) and mentions_music(text):
        return ("now_playing", "")
    if _STOP_RE.match(text) or _SHUTUP_GARBLE_RE.fullmatch(text):
        return ("stop", "")

    strong = _STRONG_PLAY_RE.match(text)
    weak = _WEAK_PLAY_RE.match(text)
    if strong or weak:
        match = strong or weak
        rest = match.group("rest").strip(" ,.")
        if not rest:
            return None
        if weak and not strong and not mentions_music(rest):
            return None
        if strong and _NON_MUSIC_OBJECT_RE.match(rest):
            return None
        rest = _TITLE_PREFIX_RE.sub("", rest).strip()
        if not rest or _GENERIC_MUSIC_RE.match(rest) or _GENERIC_MUSIC_RE.match(
            match.group("rest").strip(" ,.")
        ):
            return ("play", "")
        return ("play", rest)

    return None


def handle_music_voice(user_text: str, *, device_id: str = "") -> MusicVoiceResult:
    """Run a spoken music command and return the reply NiNO should speak."""
    from music_service import MusicNoDeviceError, get_music_service
    from music_source import (
        MusicNotConfiguredError,
        MusicNotFoundError,
        MusicUnavailableError,
    )

    service = get_music_service()
    playing = service.is_playing(device_id)

    parsed = parse_music_command(user_text)
    if parsed is None and playing and looks_like_stop_while_playing(user_text):
        parsed = ("stop", "")
    if parsed is None:
        return MusicVoiceResult(handled=False)
    action, query = parsed

    # Bare "stop" / "shut up" only claims the turn while THIS device is playing.
    if action == "stop" and not mentions_music(user_text) and not playing:
        return MusicVoiceResult(handled=False)

    try:
        if action == "play":
            if not query:
                last = service.last_track(device_id)
                query = last.spoken() if last else _DEFAULT_PLAY_QUERY
            track = service.play(device_id, query)
            return MusicVoiceResult(
                handled=True,
                reply=f"Playing {track.spoken()}.",
                reply_path="music_play",
            )
        if action == "stop":
            if not playing:
                return MusicVoiceResult(
                    handled=True,
                    reply="Nothing is playing on this speaker.",
                    reply_path="music_stop",
                )
            service.stop(device_id)
            return MusicVoiceResult(
                handled=True,
                reply="Okay, stopping the music.",
                reply_path="music_stop",
            )
        if action == "now_playing":
            session = service.current(device_id)
            reply = (
                f"This is {session.track.spoken()}."
                if session
                else "Nothing is playing right now."
            )
            return MusicVoiceResult(
                handled=True, reply=reply, reply_path="music_now_playing"
            )
    except MusicNotFoundError:
        return MusicVoiceResult(
            handled=True,
            reply=(
                f"I could not find {query}." if query else "I could not find that song."
            ),
            reply_path="music_not_found",
        )
    except MusicNotConfiguredError as exc:
        logger.warning("Music not configured: %s", exc)
        return MusicVoiceResult(
            handled=True,
            reply=(
                "I cannot play music on my speaker yet. My music firmware is not "
                "installed."
            ),
            reply_path="music_unavailable",
        )
    except MusicNoDeviceError as exc:
        logger.warning("Music device unreachable: %s", exc)
        return MusicVoiceResult(
            handled=True,
            reply="I could not reach my speaker to start the music.",
            reply_path="music_no_device",
        )
    except MusicUnavailableError as exc:
        logger.warning("Music unavailable (%s): %s", action, exc)
        return MusicVoiceResult(
            handled=True,
            reply="I cannot get that song right now. Please try again in a moment.",
            reply_path="music_unavailable",
        )

    return MusicVoiceResult(handled=False)
