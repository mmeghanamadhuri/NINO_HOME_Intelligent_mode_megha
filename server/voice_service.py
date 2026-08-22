"""STT (ElevenLabs Scribe / Whisper) + LLM (Ollama) + WAV TTS for /ws/voice and helpers.

Voice WebSocket input may be WAV or raw 16-bit mono PCM at 16 kHz.
"""

from __future__ import annotations

import io
import json
import os
import random
import re
import time
import wave
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Literal

import numpy as np
import requests

logger = logging.getLogger(__name__)

from llm_service import (
    answer_conversation_recap,
    answer_identity_question,
    answer_last_user_question,
    answer_recap_contextual_question,
    answer_voice_query,
    DEFAULT_MODEL,
    DEFAULT_OLLAMA_URL,
    extract_recap_focus_topic,
    extract_recap_follow_up_question,
    is_assumed_prior_topic_question,
    is_conversation_recap_question,
    is_last_question_query,
    last_user_question_from_history,
    recap_topic_not_found_reply,
)
from memory_filters import (
    is_bare_thank_you_stt,
    is_likely_tts_echo,
    is_unintelligible_stt,
    is_whisper_silence_hallucination,
    query_needs_recent_context,
)
from eye_expression import infer_eye_expression_for_response
from pipeline_log import begin_pipeline, log_http, pipeline_log
from tts_service import last_tts_synthesis_info, synthesize_sapi_wav_bytes
from wav_resample import (
    normalize_voice_input_bytes,
    resample_wav_bytes_to_mono_16bit,
    wav_pcm_duration_seconds,
)

# Voice assistant path uses 16 kHz on device (ESP-SR WakeNet + VAD); face TTS stays 22050 in tts_service.
VOICE_ASSIST_PLAYBACK_HZ = 16000


def _wav_seconds(wav_bytes: bytes | None) -> float:
    """Audio duration from the WAV header (not a 44-byte size guess)."""
    return round(max(0.0, wav_pcm_duration_seconds(wav_bytes or b"")), 3)

CameraIdentityState = Literal["recognized", "unknown", "no_face"]

_IDENTITY_QUESTION_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bwho am i\b",
        r"\bwhat(?:'s| is) my name\b",
        r"\bdo you know me\b",
        r"\bdo you know my name\b",
        r"\bdo you know who i am\b",
        r"\bwho is this\b",
        r"\bidentify me\b",
        r"\brecogni[sz]e me\b",
        r"\bwhat do you call me\b",
        r"\bwhat(?:'s| is) my identity\b",
        r"^my name[.!?…]*\s*$",
    )
)

_LOCAL_TIME_QUESTION_PATTERN = re.compile(
    r"\b(?:"
    r"what time is it"
    r"|what(?:'s| is) (?:the )?time(?: now)?"
    r"|what(?:'s)? (?:the )?time(?: now)?"
    r"|tell me (?:the )?time"
    r"|current time"
    r"|time now"
    r")\b",
    re.IGNORECASE,
)

_WEATHER_QUESTION_PATTERN = re.compile(
    r"\b(?:weather|forecast|temperature outside|will it rain|is it raining|"
    r"rain(?:ing)? outside|wind(?:y)? outside|outside conditions)\b",
    re.IGNORECASE,
)

_FOOTBALL_QUESTION_PATTERN = re.compile(
    r"\b(?:fifa|football|soccer|world cup|champions league|premier league|"
    r"la liga|bundesliga|serie a|live score|match score|score update|"
    r"football update)\b",
    re.IGNORECASE,
)
_FOOTBALL_JOKE_PATTERN = re.compile(
    r"\b(?:football|soccer)\s+joke\b|\b(?:tell|say|give|share)\b.{0,24}\b"
    r"(?:joke|funny one)\b",
    re.IGNORECASE,
)
_JOKE_REQUEST_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(?:tell|give|say|send|share|crack)(?:\s+me)?\s+(?:a |an |another |some )?(?:joke|jokes)\b",
        r"\b(?:got|have|know)\s+(?:a |an |another |any )?(?:joke|jokes)\b",
        r"\bmake me laugh\b",
        r"\bcheer me up\b",
        r"\bsomething funny\b",
        r"\bfunny (?:story|joke)\b",
        r"^(?:a |an |another )?joke\??[.!…]*\s*$",
        r"^jokes?\??[.!…]*\s*$",
    )
)
_JOKE_NEGATIVE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bit(?:'s| is) (?:a |only a |just a )?joke\b",
        r"\bjust (?:a joke|kidding|joking)\b",
        r"\bi(?:'m| am) (?:just )?joking\b",
        r"\byou(?:'re| are) a joke\b",
        r"\bnot a joke\b",
    )
)
JOKES: tuple[str, ...] = (
    "Why don't skeletons fight each other? They don't have the guts.",
    "Why did the scarecrow win an award? Because he was outstanding in his field.",
    'I told my computer I needed a break. It said, "No problem, I\'ll go to sleep."',
    "Why don't eggs tell jokes? They'd crack each other up.",
    "Parallel lines have so much in common. It's a shame they'll never meet.",
    "Why did the math book look sad? Because it had too many problems.",
    "Why did the coffee file a police report? It got mugged.",
    "What do you call fake spaghetti? An impasta.",
    "Why can't your nose be 12 inches long? Because then it would be a foot.",
    "Why did the bicycle fall over? Because it was two-tired.",
    "I only know 25 letters of the alphabet. I don't know Y.",
    "What do you call cheese that isn't yours? Nacho cheese.",
    "Why did the tomato blush? Because it saw the salad dressing.",
    "Why don't scientists trust atoms? Because they make up everything.",
    "What's orange and sounds like a parrot? A carrot.",
    "Why was the computer cold? It left its Windows open.",
    "Why did the golfer bring an extra pair of pants? In case he got a hole in one.",
    "I used to play piano by ear. Now I use my hands.",
    "Why did the cookie go to the hospital? Because it felt crummy.",
    "Why don't programmers like nature? It has too many bugs.",
    "Debugging: being the detective in a crime movie where you're also the murderer.",
    'My password is "incorrect." So whenever I forget it, the computer reminds me, "Your password is incorrect."',
    "Why did the Wi-Fi break up with the router? There was no connection.",
    'I asked the librarian if the library had books on paranoia. She whispered, "They\'re right behind you."',
    "Why did the student eat his homework? Because the teacher said it was a piece of cake.",
    'I told my boss I was late because of traffic. He said, "You work from home."',
    "Why did the keyboard break up with the mouse? It felt like it was being clicked with everyone.",
    "My phone battery lasts longer than my motivation.",
    "I finally cleaned my room. Now I can't find anything.",
    "Why was the calendar so popular? Because it had so many dates.",
    # Short, plain-spoken one-liners — easy to follow the first time you hear them.
    "Why did the banana go to the doctor? Because it was not peeling well.",
    "What do you call a sleeping dinosaur? A dino-snore.",
    "Why did the teddy bear stop eating? Because it was already stuffed.",
    "What do you call a bear with no teeth? A gummy bear.",
    "Why did the picture go to jail? Because it was framed.",
    "What did the ocean say to the beach? Nothing, it just waved.",
    "Why are elevator jokes so good? They work on so many levels.",
    "How does the moon cut his hair? Eclipse it.",
    "Why did the orange stop rolling down the hill? It ran out of juice.",
    "What do clouds wear under their shorts? Thunderpants.",
    "Why did the belt go to jail? Because it held up a pair of trousers.",
    "What do you call a pig that does karate? A pork chop.",
    "Why did the barber win the race? Because he took a short cut.",
    "Why was the broom late? Because it over-swept.",
    "What is a computer's favourite snack? Microchips.",
    "Why do bees have sticky hair? Because they use honeycombs.",
    "What do you call two birds in love? Tweethearts.",
    "Why did the egg hide? Because it was a little chicken.",
    "What did one wall say to the other wall? I will meet you at the corner.",
    "Why did the cow go to space? To see the Milky Way.",
    "What do you call a duck that gets all A's? A wise quacker.",
    "Why did the sun go to school? To get a little brighter.",
)
_joke_deck: list[str] = []
_last_joke: str | None = None

# Spoken lead-ins / sign-offs so every joke lands as excited and playful
# instead of the flat "Sure! <joke>" the device used to say every time.
JOKE_OPENERS: tuple[str, ...] = (
    "Ooh, I love this one!",
    "Yes! Get ready to laugh!",
    "Ha, okay, here we go!",
    "Oh, this is my favourite one!",
    "Right, brace yourself!",
    "Coming right up!",
    "This one always gets me!",
    "Oh yes, I have a good one!",
)
JOKE_CLOSERS: tuple[str, ...] = (
    "Ha ha! I love that one.",
    "Ha! Classic.",
    "Ha ha! Got you there.",
    "Ha! That one always makes me giggle.",
    "Ha ha! Tell me that was not funny!",
    "Ha! Want another one?",
)
_joke_opener_deck: list[str] = []
_joke_closer_deck: list[str] = []
_WORLD_CUP_FAVOURITE_PATTERN = re.compile(
    r"\b(?:fifa\s+)?world\s+cup\b.{0,80}\b(?:favo(?:u)?rite|rooting for|"
    r"supporting)\b|\b(?:favo(?:u)?rite|rooting for|supporting)\b.{0,80}\b"
    r"(?:fifa\s+)?world\s+cup\b",
    re.IGNORECASE,
)
NINO_FAVOURITE_WORLD_CUP_TEAM = "Brazil"

_SERVO_360_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bmake a 360\b",
        r"\bdo a 360\b",
        r"\bmake (?:a )?360\b",
        r"\bdo (?:a )?360\b",
        r"\bspin 360\b",
        r"\bspin a 360\b",
        r"\b(?:make|do) (?:a )?(?:three[\s-]?sixty|360)\b",
        r"\b(?:spin|rotate|turn)(?: around)? (?:a )?360\b",
        r"\b360 (?:degree|degrees|spin|rotation)\b",
        r"\bfull 360\b",
        r"\b(?:servo|motor|head).{0,20}(?:spin|rotate|360)\b",
        r"\b(?:spin|rotate|360).{0,20}(?:servo|motor|head)\b",
    )
)

_FACE_TRACK_OFF_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bstop tracking(?:\s+my\s+face)?\b",
        r"\b(?:don'?t|do not|dont)\s+track(?:\s+my\s+face)?\b",
        r"\bstop following my face\b",
        r"\bturn off (?:the\s+)?(?:face\s+)?tracking\b",
        r"\bdisable (?:face\s+)?tracking\b",
    )
)

_FACE_TRACK_ON_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\btrack my face\b",
        r"\bstart tracking(?:\s+my\s+face)?\b",
        r"\bsee me while i(?:'?m| am) (?:talking|speaking)\b",
        r"\blook at me while i(?:'?m| am) (?:talking|speaking)\b",
        r"\bwatch me while i(?:'?m| am) (?:talking|speaking)\b",
        r"\bwatch me while i speak\b",
        r"\bkeep looking at (?:me|my face)\b",
        r"\bfollow my face\b",
        r"\bwatch my face\b",
        r"\benable (?:face\s+)?tracking\b",
        r"\bturn on (?:the\s+)?(?:face\s+)?tracking\b",
    )
)

_WHAT_DO_YOU_SEE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bwhat do you see\b",
        r"\bwhat can you see\b",
        r"\bwhat are you seeing\b",
        r"\btell me what you see\b",
        r"\bwhat(?:'s| is) in front of you\b",
        r"\bwhat do you see (?:now|right now|in front of you)\b",
        r"\bdescribe what you see\b",
        r"\bwhat(?:'s| is) around you\b",
    )
)

_VOLUME_SET_PATTERN = re.compile(
    r"\b(?:set|change|make|keep|increase|decrease|raise|lower|turn)\b.*\bvolume\b"
    r".*?\b(?:to|at)\s+([a-z0-9\s-]+)(?:\s*percent)?\b",
    re.IGNORECASE,
)
_VOLUME_STEP_PATTERN = re.compile(
    r"\b(increase|decrease|raise|lower)\s+(?:the\s+)?(?:speaker\s+)?volume\b"
    r"(?:\s+by\s+([a-z0-9\s-]+?)(?:\s*percent)?)?\b"
    r"|\bvolume\s+(up|down)\b(?:\s+by\s+([a-z0-9\s-]+?)(?:\s*percent)?)?\b",
    re.IGNORECASE,
)
_NUMBER_WORDS: dict[str, int] = {
    "zero": 0,
    "ten": 10,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
    "hundred": 100,
}

# Seconds after TTS is sent before POST /servo/360 (lets confirmation play first).
SERVO_360_TRIGGER_DELAY_SECONDS = float(os.environ.get("SERVO_360_TRIGGER_DELAY_SECONDS", "2.0"))


@dataclass
class VoiceReplyMeta:
    trigger_servo_360: bool = False
    # Reuses ESP prompt_ack path: after TTS, chime + open mic (no wake word).
    prompt_medical_ack: bool = False
    # After TTS, P4 raises GPIO 5 so Sirena closes the mics.
    end_session: bool = False
    session_id: str = ""
    eye_expression: str | None = None
    motion: list[str] | None = None
    registered_face_name: str | None = None
    face_reg_relisten: bool = False
    device_id: str = ""
    # After this WAV, firmware pans left/right and asks for pose reports.
    look_scan: bool = False
    # None = unchanged. True/False is sent on the WS reply so the P4 enables
    # pan/tilt tracking locally (HTTP POST to /face/track is best-effort).
    face_track: bool | None = None
    # Per-stage latency info for this query (stt/reply/tts seconds, heard text,
    # reply path, audio sizes). Filled by process_voice_wav; logged by app.py.
    timings: dict[str, Any] = field(default_factory=dict)


# After a valid wake, keep the conversation open until goodbye — except music.
# Play/stop are one-shot: "Playing X." then the mic closes. A new Ok Nino is
# required for the next command so song turns never become a chat.
MUSIC_SESSION_END_REPLY_PATHS = frozenset(
    {
        "music_play",
        "music_stop",
        "music_now_playing",
        "music_not_found",
        "music_unavailable",
        "music_no_device",
        "music_resume",
    }
)

SESSION_END_REPLY_PATHS = frozenset(
    {
        "goodbye",
        "wake_reject",
        "stt_empty",
        "stt_silent",
        "stt_rejected",
        "silent_skip",
    }
) | MUSIC_SESSION_END_REPLY_PATHS

# Kept for older callers / docs; session lifetime is no longer gated on this list.
CONTINUE_LISTEN_REPLY_PATHS = frozenset(
    {
        "llm",
        "identity_llm",
        "memory_llm_store",
        "memory_llm_recall",
        "recap",
        "recap_answer",
        "recap_not_found",
        "recap_blocked_no_face",
        "last_question",
        "joke",
        "joke_and_time",
        "greeting",
        "smalltalk",
        "math",
        "session_greet",
        "session_register_offer",
        "face_registration",
        "face_track",
        "look_scan",
        "volume",
    }
)

# Music replies are one-shot: "Playing X." / "Okay, stopping the music."
# Do not reopen the mic or they turn into a conversation.

# Turns stored in the per-device session buffer until the user says goodbye.
DEVICE_SESSION_LOG_PATHS = CONTINUE_LISTEN_REPLY_PATHS | {"goodbye", "math"}

# Pure time-of-day / hello greetings — answered from the PC clock, not echoed from STT.
_TIME_OF_DAY_GREETING_RE = re.compile(
    r"^\s*(?:hey|hi|hello)"
    r"(?:\s*,?\s*(?:there|everyone|all|folks|guys|team))?"
    r"[.!?…]*\s*$"
    r"|^\s*(?:good\s+)?(?:morning|afternoon|evening|night)"
    r"(?:\s*,?\s*(?:everyone|all|there|folks|guys|team|[A-Za-z]{2,20}))?"
    r"[.!?…]*\s*$",
    re.IGNORECASE,
)

_CONVERSATION_GOODBYE_RE = re.compile(
    r"\b(?:"
    r"good\s*bye|goodbye|bye[\s-]*bye|bye|"
    r"see\s+you(?:\s+(?:later|soon|tomorrow))?|"
    r"talk\s+(?:to\s+you\s+)?later|"
    r"that(?:'s| is)\s+all|"
    r"i(?:'m| am)\s+done|"
    r"stop\s+listening|"
    r"end\s+(?:the\s+)?(?:conversation|chat)"
    r")\b",
    re.IGNORECASE,
)

# Bare "stop" ends the session; "stop the music" is handled by music_stop instead.
_CONVERSATION_STOP_RE = re.compile(
    r"^\s*(?:please\s+)?stop(?:\s+(?:now|please))?[.!?]*\s*$",
    re.IGNORECASE,
)

_GOODBYE_REPLIES = (
    "Goodbye! See you later.",
    "Bye! See you soon.",
    "Take care! Talk soon.",
)

# Wake session: STT often misspells Nino. Match near the start, then strip.
# "Ok Nino" (and hey/hi/hello + name) or a leading "Hello" both start a session.
_WAKE_RE = re.compile(
    r"\b(?P<phrase>(?:ok(?:ay)?|hey|hi|hello)\s+"
    r"(?:nino|nano|neno|nina|nenu|neeno|knee\s*no|you know))\b",
    re.IGNORECASE,
)
_WAKE_HELLO_RE = re.compile(
    r"^(?:(?:um+|uh+|er+|ah+|mm+)\s+){0,3}"
    r"(?P<phrase>hell+o|hallo|hullo)(?:\s+there)?\b",
    re.IGNORECASE,
)
# Whisper often drops "Nino" on wake clips: "ok nino hello" -> "okay hello" / "okay no hello".
_WAKE_OKAY_HELLO_RE = re.compile(
    r"^(?:(?:um+|uh+|er+|ah+|mm+)\s+){0,3}"
    r"(?P<phrase>ok(?:ay)?)\s+"
    r"(?:(?:no|now|know|nino|nano|neno|nina|nenu|neeno|knee\s*no|you know)\s+)?"
    r"hell+o\b",
    re.IGNORECASE,
)
# "ok nino" alone is often heard as "okay now" / "okay no".
_WAKE_OKAY_NINO_MISHEAR_RE = re.compile(
    r"^(?:(?:um+|uh+|er+|ah+|mm+)\s+){0,3}"
    r"(?P<phrase>ok(?:ay)?)\s+(?:no|now|know)\b",
    re.IGNORECASE,
)
_WAKE_NAME_RE = re.compile(
    r"\b(?P<phrase>nino|nano|neno|nina)\b",
    re.IGNORECASE,
)

_WAKE_LISTEN_REPLY = "Yes?"


def _normalize_wake_text(text: str) -> str:
    t = str(text or "").lower().replace("'", "")
    t = re.sub(r"[,.!?]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def extract_wake_and_command(text: str) -> tuple[bool, str, str]:
    """Return (found, command_after_wake, matched_phrase).

    Used only to strip a leading wake phrase from STT, not to reject queries.
    """
    raw = str(text or "").strip()
    norm = _normalize_wake_text(raw)
    if not norm:
        return False, "", ""
    match = _WAKE_RE.search(norm)
    if match is None:
        match = _WAKE_HELLO_RE.search(norm)
    if match is None:
        match = _WAKE_OKAY_HELLO_RE.search(norm)
    if match is None:
        match = _WAKE_OKAY_NINO_MISHEAR_RE.search(norm)
    if match is None:
        # "nino" alone in the first few words still counts as the wake name.
        match = _WAKE_NAME_RE.search(norm)
        if match is not None and match.start() > 24:
            match = None
    if match is None:
        return False, raw, ""
    rest = norm[match.end() :].strip(" ,.-")
    return True, rest, _normalize_wake_text(match.group("phrase"))


def log_nino_voice(stage: str, *, turn: object = None, **fields: object) -> None:
    """One-line P4/server log. Grep: NINO VOICE"""
    parts: list[str] = []
    for key, value in fields.items():
        if value is None:
            continue
        text = str(value)
        if text == "" or any(ch.isspace() for ch in text):
            text = repr(text)
        parts.append(f"{key}={text}")
    logger.info(
        "NINO VOICE | turn=%s | %-12s | %s",
        "-" if turn is None else turn,
        stage[:12],
        " ".join(parts),
    )


def _log_voice_banner(title: str, **fields: object) -> None:
    stage = str(title or "INFO").replace(" ", "_")
    turn = fields.pop("turn", None)
    log_nino_voice(stage, turn=turn, **fields)


_REJECTED_WAKE_PATH = Path(__file__).resolve().parent / "data" / "rejected_wake.log"


def _record_rejected_wake(
    *,
    device_id: str,
    heard: str,
    reason: str,
    audio_s: float = 0.0,
) -> None:
    heard_s = str(heard or "").strip()
    logger.warning(
        "NINO VOICE | turn=- | REJECT   | device=%s heard=%r reason=%s",
        device_id or "-",
        heard_s or "(empty STT)",
        reason,
    )
    try:
        _REJECTED_WAKE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_REJECTED_WAKE_PATH, "a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "timestamp": datetime.now().isoformat(timespec="seconds"),
                        "device_id": device_id or "",
                        "heard": heard_s,
                        "reason": reason,
                        "audio_in_seconds": round(float(audio_s), 3),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    except Exception:
        logger.exception("Could not append rejected wake log")


def is_conversation_goodbye(user_text: str) -> bool:
    """True when the user is ending the chat (do not reopen the mic)."""
    text = str(user_text or "").strip()
    if not text:
        return False
    if _CONVERSATION_STOP_RE.match(text):
        return True
    return bool(_CONVERSATION_GOODBYE_RE.search(text))


def utterance_ends_session(reply_path: str, user_text: str) -> bool:
    """Streaming sessions close only on goodbye/stop, after TTS plays."""
    if reply_path == "goodbye":
        return True
    return is_conversation_goodbye(user_text)


def conversation_goodbye_reply() -> str:
    """Short farewell only — no follow-up question (conversation ends after this)."""
    return random.choice(_GOODBYE_REPLIES)


NAME_INJECT_RATE = 0.7


def maybe_address_by_name(
    reply: str,
    name: str | None,
    *,
    is_guest: bool = False,
    rng: random.Random | None = None,
    chance: float = NAME_INJECT_RATE,
) -> str:
    """Include the user's name in about 70% of replies (not every sentence)."""
    text = str(reply or "").strip()
    cleaned = (name or "").strip()
    if not text or not cleaned or is_guest:
        return text
    if cleaned.lower().startswith("guest"):
        return text
    if re.search(rf"\b{re.escape(cleaned)}\b", text, re.I):
        return text
    pick = rng.random() if rng is not None else random.random()
    if pick >= chance:
        return text
    if text[0].isupper() and len(text) > 1:
        rest = text[0].lower() + text[1:]
    else:
        rest = text
    return f"{cleaned}, {rest}"


def synthesize_session_open_wav(
    reply: str,
    *,
    session_id: str = "",
    device_id: str = "",
    reply_path: str = "session_greet",
    eye_expression: str | None = None,
    user_name: str | None = None,
) -> tuple[bytes, VoiceReplyMeta]:
    """Greeting / register-offer TTS at stream session open (end_session false)."""
    from servo_tts_motion import motion_actions_for_reply

    meta = VoiceReplyMeta(session_id=session_id, device_id=device_id)
    meta.end_session = False
    # Keep the stream session in continue-listen after GREET so the board
    # treats this like a spoken reply, not a silent skip.
    meta.prompt_medical_ack = True
    from voice_listen_state import mark_session_open

    mark_session_open(session_id, device_id)
    # Identified greet -> heart; hunt / register-offer -> no LCD emoji.
    inferred = infer_eye_expression_for_response(reply, reply_path=reply_path)
    if inferred == "heart" or (eye_expression or "").strip().lower() == "heart":
        meta.eye_expression = "heart"
    else:
        meta.eye_expression = None
    meta.motion = motion_actions_for_reply(reply, reply_path=reply_path)
    wav, _voice = synthesize_sapi_wav_bytes(reply)
    wav_out = resample_wav_bytes_to_mono_16bit(wav, VOICE_ASSIST_PLAYBACK_HZ)
    meta.timings = {
        "heard": "",
        "reply_text": reply[:200],
        "reply_path": reply_path,
        "session_open": True,
        "voice_viewer": user_name or "",
        "audio_out_seconds": round(_wav_seconds(wav_out), 2),
        "audio_out_bytes": len(wav_out),
        "eye_expression": eye_expression or "",
        "motion": meta.motion,
    }
    return wav_out, meta


def synthesize_idle_goodbye_wav(
    *,
    session_id: str = "",
    device_id: str = "",
) -> tuple[bytes, VoiceReplyMeta]:
    """Farewell TTS after 30s of no speech on an open stream session."""
    begin_pipeline(device_id=device_id, session="stream")
    reply = conversation_goodbye_reply()
    meta = VoiceReplyMeta(
        end_session=True,
        session_id=session_id,
        device_id=device_id,
    )
    meta.prompt_medical_ack = False
    wav, _voice = synthesize_sapi_wav_bytes(reply)
    wav_out = resample_wav_bytes_to_mono_16bit(wav, VOICE_ASSIST_PLAYBACK_HZ)
    meta.timings = {
        "heard": "",
        "reply_text": reply[:200],
        "reply_path": "goodbye",
        "idle_timeout": True,
        "audio_out_seconds": round(_wav_seconds(wav_out), 2),
        "audio_out_bytes": len(wav_out),
    }
    log_nino_voice(
        "SESSION",
        state="end",
        path="goodbye",
        reason="idle_timeout",
        device=device_id or "-",
        session_id=session_id,
        next="GPIO 5 after TTS, then wait for Ok Nino",
    )
    from conversation_sessions import end_session as persist_end_session
    from device_session import clear_device_session
    from math_voice import clear_math_quiz

    clear_device_session(device_id)
    clear_math_quiz(device_id)
    if session_id:
        persist_end_session(
            session_id,
            device_id=device_id,
            reason="idle_timeout",
        )
    return wav_out, meta


def should_continue_listen_after_reply(reply_path: str, user_text: str) -> bool:
    """Keep the conversation open until the user says goodbye."""
    enabled = os.environ.get("VOICE_CONTINUE_LISTEN", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    if not enabled:
        return False
    if reply_path in SESSION_END_REPLY_PATHS:
        return False
    if is_conversation_goodbye(user_text):
        return False
    return True


def _mark_continue_listen(meta: VoiceReplyMeta, reply_path: str, user_text: str) -> None:
    from voice_listen_state import mark_session_closed, mark_session_open

    turn = meta.timings.get("turn")
    meta.end_session = utterance_ends_session(reply_path, user_text)
    if meta.end_session:
        meta.prompt_medical_ack = False
        mark_session_closed(meta.session_id, meta.device_id)
        log_nino_voice(
            "SESSION",
            turn=turn,
            state="end",
            path=reply_path,
            heard=str(user_text or "")[:120],
            next="GPIO 5 after TTS, then wait for Ok Nino",
        )
        return
    if should_continue_listen_after_reply(reply_path, user_text):
        meta.prompt_medical_ack = True
        mark_session_open(meta.session_id, meta.device_id)
        log_nino_voice(
            "SESSION",
            turn=turn,
            state="open",
            path=reply_path,
            heard=str(user_text or "")[:120],
            next="mic stays open until goodbye",
        )
    else:
        mark_session_closed(meta.session_id, meta.device_id)
        log_nino_voice(
            "SESSION",
            turn=turn,
            state="end",
            path=reply_path,
            heard=str(user_text or "")[:120],
            next="waiting for a new Ok Nino",
        )


# Roughly 2–3 personalized voice replies per 10–20 (override with VOICE_PERSONALIZE_PROB).
DEFAULT_VOICE_PERSONALIZE_PROB = 0.18


@dataclass
class VoiceSettings:
    ollama_url: str = DEFAULT_OLLAMA_URL
    ollama_model: str = DEFAULT_MODEL
    whisper_model: str = "small"
    whisper_language: str | None = "en"
    # auto | cuda | cpu — resolved at load time against CTranslate2 CUDA.
    whisper_device: str = "cuda"
    whisper_compute_type: str = "auto"
    whisper_device_index: int = 0
    whisper_runtime_device: str = ""
    whisper_runtime_compute_type: str = ""
    # "elevenlabs" | "openai_whisper" (cloud Whisper API) | "whisper" (local faster-whisper).
    stt_provider: str = "whisper"
    elevenlabs_api_key: str = ""
    elevenlabs_stt_model: str = "scribe_v1"
    openai_api_key: str = ""
    openai_api_base: str = "https://api.openai.com/v1"
    openai_whisper_model: str = "whisper-1"
    # 16 kHz 16-bit mono WAV: 2 bytes/sample + header. Cover 60s registration.
    max_request_bytes: int = 2_100_000
    max_words_reply: int = 45
    recap_max_words: int = 55
    personalize_prob: float = DEFAULT_VOICE_PERSONALIZE_PROB


SETTINGS = VoiceSettings()
_WHISPER_MODEL: Any = None
_CTRANSLATE2_CUDA_COUNT: int | None = None
_WHISPER_CUDA_FALLBACK_LOGGED = False


DEFAULT_MIN_SPEECH_ENERGY = 5


def min_speech_energy() -> int:
    raw = os.environ.get("VOICE_MIN_ENERGY", str(DEFAULT_MIN_SPEECH_ENERGY)).strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_MIN_SPEECH_ENERGY


def long_clip_min_mean_energy() -> int:
    raw = os.environ.get("VOICE_LONG_CLIP_MIN_MEAN_ENERGY", "18").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 18


def long_clip_peak_override_energy() -> int:
    """When peak frame energy exceeds this, run STT even on long low-mean clips."""
    raw = os.environ.get("VOICE_LONG_CLIP_PEAK_OVERRIDE", "80").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 80


def speech_like_clip(peak_energy: int, mean_energy: int) -> bool:
    """True when peak energy shows real speech despite a low clip mean (VAD tail)."""
    return int(peak_energy) >= long_clip_peak_override_energy()


def long_clip_min_seconds() -> float:
    raw = os.environ.get("VOICE_LONG_CLIP_MIN_SECONDS", "6.0").strip()
    try:
        return max(1.0, float(raw))
    except ValueError:
        return 6.0


def quiet_clip_min_mean_energy() -> int:
    raw = os.environ.get("VOICE_QUIET_CLIP_MIN_MEAN_ENERGY", "12").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 12


def quiet_clip_min_seconds() -> float:
    raw = os.environ.get("VOICE_QUIET_CLIP_MIN_SECONDS", "3.0").strip()
    try:
        return max(0.5, float(raw))
    except ValueError:
        return 3.0


def _preserve_continue_on_skip(
    *,
    session: str,
    session_id: str,
    device_id: str,
) -> bool:
    from voice_listen_state import should_preserve_continue_listen

    return should_preserve_continue_listen(
        session_id,
        device_id,
        session_kind=session,
    )


def wav_peak_frame_energy(wav_bytes: bytes, frame_ms: int = 20) -> int:
    """Mean-abs peak over 20 ms frames — same units as the P4 Aux energy gate."""
    if not wav_bytes or len(wav_bytes) <= 44:
        return 0
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            nch = wf.getnchannels()
            sw = wf.getsampwidth()
            sr = wf.getframerate()
            raw = wf.readframes(wf.getnframes())
    except Exception:
        return 0
    if sw != 2 or nch < 1 or sr <= 0 or not raw:
        return 0
    pcm = np.frombuffer(raw, dtype=np.int16)
    if nch > 1:
        pcm = pcm.reshape(-1, nch).mean(axis=1).astype(np.int32)
    else:
        pcm = pcm.astype(np.int32)
    frame = max(1, int(sr * frame_ms / 1000))
    peak = 0
    for i in range(0, len(pcm) - frame + 1, frame):
        energy = int(np.mean(np.abs(pcm[i : i + frame])))
        if energy > peak:
            peak = energy
    if peak == 0 and len(pcm) > 0:
        peak = int(np.mean(np.abs(pcm)))
    return peak


def wav_mean_frame_energy(wav_bytes: bytes, frame_ms: int = 20) -> int:
    """Mean 20 ms frame energy — catches long ambient/noise clips Whisper hallucinates on."""
    if not wav_bytes or len(wav_bytes) <= 44:
        return 0
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            nch = wf.getnchannels()
            sw = wf.getsampwidth()
            sr = wf.getframerate()
            raw = wf.readframes(wf.getnframes())
    except Exception:
        return 0
    if sw != 2 or nch < 1 or sr <= 0 or not raw:
        return 0
    pcm = np.frombuffer(raw, dtype=np.int16)
    if nch > 1:
        pcm = pcm.reshape(-1, nch).mean(axis=1).astype(np.int32)
    else:
        pcm = pcm.astype(np.int32)
    frame = max(1, int(sr * frame_ms / 1000))
    if len(pcm) < frame:
        return int(np.mean(np.abs(pcm))) if len(pcm) else 0
    total = 0
    count = 0
    for i in range(0, len(pcm) - frame + 1, frame):
        total += int(np.mean(np.abs(pcm[i : i + frame])))
        count += 1
    return total // count if count else 0


def clip_peak_energy(wav_bytes: bytes, reported: int | None = None) -> int:
    measured = wav_peak_frame_energy(wav_bytes)
    if reported is None:
        return measured
    try:
        return max(measured, int(reported))
    except (TypeError, ValueError):
        return measured


def minimal_voice_reply_wav() -> bytes:
    """Short silent 16-bit mono WAV at VOICE_ASSIST_PLAYBACK_HZ — ESP parse always succeeds."""
    sr = VOICE_ASSIST_PLAYBACK_HZ
    ms = 120
    n = max(1, sr * ms // 1000)
    silence = b"\x00\x00" * n
    bio = io.BytesIO()
    with wave.open(bio, "wb") as wo:
        wo.setnchannels(1)
        wo.setsampwidth(2)
        wo.setframerate(sr)
        wo.writeframes(silence)
    return bio.getvalue()


def _ctranslate2_cuda_device_count() -> int:
    """How many GPUs CTranslate2 (faster-whisper) can actually use."""
    global _CTRANSLATE2_CUDA_COUNT
    if _CTRANSLATE2_CUDA_COUNT is not None:
        return _CTRANSLATE2_CUDA_COUNT
    try:
        import ctranslate2

        _CTRANSLATE2_CUDA_COUNT = int(ctranslate2.get_cuda_device_count())
    except Exception:
        _CTRANSLATE2_CUDA_COUNT = 0
    return _CTRANSLATE2_CUDA_COUNT


def ctranslate2_cuda_available() -> bool:
    return _ctranslate2_cuda_device_count() > 0


def resolve_whisper_device(preferred: str | None = None, *, cuda_available: bool | None = None) -> str:
    """Pick cuda vs cpu. `auto` uses CTranslate2 CUDA, not torch."""
    raw = (preferred if preferred is not None else SETTINGS.whisper_device).strip().lower()
    if raw in {"gpu"}:
        raw = "cuda"
    cuda = ctranslate2_cuda_available() if cuda_available is None else cuda_available
    if raw in {"", "auto"}:
        return "cuda" if cuda else "cpu"
    if raw == "cuda":
        if cuda:
            return "cuda"
        global _WHISPER_CUDA_FALLBACK_LOGGED
        if not _WHISPER_CUDA_FALLBACK_LOGGED:
            logger.warning(
                "WHISPER_DEVICE=cuda but CTranslate2 reports no GPU; using CPU. "
                "Install a CUDA ctranslate2 wheel for this machine."
            )
            _WHISPER_CUDA_FALLBACK_LOGGED = True
        return "cpu"
    return "cpu"


def resolve_whisper_compute_type(
    device: str, preferred: str | None = None
) -> str:
    raw = (
        preferred if preferred is not None else SETTINGS.whisper_compute_type
    ).strip().lower()
    if raw in {"", "auto", "default"}:
        return "float16" if device == "cuda" else "int8"
    return raw


def whisper_runtime_status() -> dict[str, Any]:
    device = SETTINGS.whisper_runtime_device or resolve_whisper_device()
    compute = SETTINGS.whisper_runtime_compute_type or resolve_whisper_compute_type(
        device
    )
    return {
        "provider": SETTINGS.stt_provider,
        "model": SETTINGS.whisper_model,
        "requested_device": SETTINGS.whisper_device,
        "device": device,
        "device_index": SETTINGS.whisper_device_index,
        "compute_type": compute,
        "loaded": _WHISPER_MODEL is not None,
        "cuda_available": ctranslate2_cuda_available(),
        "cuda_device_count": _ctranslate2_cuda_device_count(),
    }


def _whisper_preload_enabled() -> bool:
    raw = os.environ.get("WHISPER_PRELOAD", "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return SETTINGS.stt_provider == "whisper"


def preload_whisper_model() -> bool:
    """Load faster-whisper at startup so the first voice query is not cold."""
    if not _whisper_preload_enabled():
        return False
    try:
        _ensure_whisper()
        status = whisper_runtime_status()
        logger.info(
            "Whisper preloaded: model=%s device=%s compute_type=%s",
            status["model"],
            status["device"],
            status["compute_type"],
        )
        return True
    except Exception as exc:
        logger.warning("Whisper preload failed: %s", exc)
        return False


def configure_from_environ() -> None:
    global _WHISPER_MODEL, _CTRANSLATE2_CUDA_COUNT, _WHISPER_CUDA_FALLBACK_LOGGED
    SETTINGS.ollama_url = os.environ.get("OLLAMA_URL", DEFAULT_OLLAMA_URL).strip()
    SETTINGS.ollama_model = os.environ.get("OLLAMA_MODEL", DEFAULT_MODEL).strip()
    SETTINGS.whisper_model = os.environ.get("WHISPER_MODEL", "small").strip()
    SETTINGS.whisper_device = os.environ.get("WHISPER_DEVICE", "cuda").strip() or "cuda"
    SETTINGS.whisper_compute_type = (
        os.environ.get("WHISPER_COMPUTE_TYPE", "auto").strip() or "auto"
    )
    try:
        SETTINGS.whisper_device_index = int(
            os.environ.get("WHISPER_DEVICE_INDEX", "0").strip() or "0"
        )
    except ValueError:
        SETTINGS.whisper_device_index = 0
    SETTINGS.whisper_runtime_device = ""
    SETTINGS.whisper_runtime_compute_type = ""
    _WHISPER_MODEL = None
    _CTRANSLATE2_CUDA_COUNT = None
    _WHISPER_CUDA_FALLBACK_LOGGED = False
    lang = os.environ.get("WHISPER_LANGUAGE", "en").strip()
    SETTINGS.whisper_language = None if lang.lower() in {"", "auto"} else lang
    SETTINGS.elevenlabs_api_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    SETTINGS.elevenlabs_stt_model = os.environ.get(
        "ELEVENLABS_STT_MODEL", "scribe_v1"
    ).strip()
    SETTINGS.openai_api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    SETTINGS.openai_api_base = os.environ.get(
        "OPENAI_API_BASE", "https://api.openai.com/v1"
    ).strip().rstrip("/")
    SETTINGS.openai_whisper_model = os.environ.get(
        "OPENAI_WHISPER_MODEL", "whisper-1"
    ).strip()
    provider = os.environ.get("STT_PROVIDER", "").strip().lower()
    if not provider:
        # Local GPU Whisper is the stable default; cloud STT is opt-in.
        provider = "whisper"
    SETTINGS.stt_provider = provider
    if provider == "elevenlabs" and not SETTINGS.elevenlabs_api_key:
        logger.warning(
            "STT provider is elevenlabs but ELEVENLABS_API_KEY is not set; "
            "falling back to local Whisper."
        )
        SETTINGS.stt_provider = "whisper"
    if provider in {"openai_whisper", "openai", "whisper_api"} and not SETTINGS.openai_api_key:
        logger.warning(
            "STT provider is openai_whisper but OPENAI_API_KEY is not set; "
            "falling back to local Whisper."
        )
        SETTINGS.stt_provider = "whisper"
    SETTINGS.personalize_prob = float(
        os.environ.get("VOICE_PERSONALIZE_PROB", str(DEFAULT_VOICE_PERSONALIZE_PROB))
    )
    SETTINGS.personalize_prob = min(1.0, max(0.0, SETTINGS.personalize_prob))
    SETTINGS.recap_max_words = max(
        30,
        min(80, int(os.environ.get("VOICE_RECAP_MAX_WORDS", "55"))),
    )
    status = whisper_runtime_status()
    logger.info(
        "Voice STT provider=%s whisper=%s requested=%s runtime=%s cuda=%s",
        SETTINGS.stt_provider,
        SETTINGS.whisper_model,
        SETTINGS.whisper_device,
        status["device"],
        status["cuda_available"],
    )


def _viewer_for_this_reply(viewer_name: str | None) -> str | None:
    """Randomly include the camera viewer name (~2–3 of every 10–20 voice replies)."""
    if not viewer_name:
        return None
    cleaned = viewer_name.strip()
    if not cleaned or cleaned.lower() in {"unknown", "face"}:
        return None
    if random.random() < SETTINGS.personalize_prob:
        return cleaned
    return None


def _recent_assistant_replies(
    recent_history: list[tuple[str, str]] | None,
) -> list[str]:
    if not recent_history:
        return []
    return [str(assistant_text or "").strip() for _user, assistant_text in recent_history]


def _voice_memory_context(
    *,
    device_id: str,
    memory_name: str | None,
    memory_ctx: Any,
    user_text: str,
    viewer_name: str | None,
    effective_viewer: str | None,
) -> tuple[str | None, list[tuple[str, str]], bool]:
    """Merge PostgreSQL memory with the active device session (until goodbye)."""
    from device_session import (
        format_device_session_prompt,
        get_device_session_turns,
        merge_prompt_blocks,
    )
    from memory_filters import query_needs_recent_context
    from memory_service import get_memory_service

    session_turns = get_device_session_turns(device_id)
    follow_up = bool(session_turns) or query_needs_recent_context(user_text)

    session_block = (
        format_device_session_prompt(
            session_turns,
            viewer_name=effective_viewer or memory_name or viewer_name,
        )
        if session_turns
        else None
    )

    memory_block: str | None = None
    db_history: list[tuple[str, str]] = []
    if memory_name:
        memory_svc = get_memory_service()
        if session_turns:
            facts_ctx = memory_svc.load_context(
                memory_name,
                query_text=user_text,
                include_recent=False,
            )
            memory_block = facts_ctx.prompt_block if facts_ctx else None
        elif memory_ctx:
            memory_block = memory_ctx.prompt_block or None
            db_history = list(memory_ctx.recent_history)
        else:
            loaded = memory_svc.load_context(memory_name, query_text=user_text)
            memory_block = loaded.prompt_block if loaded else None
            db_history = list(loaded.recent_history) if loaded else []

    recent_history = session_turns or db_history
    return merge_prompt_blocks(memory_block, session_block), recent_history, follow_up


def _live_memory_viewer_name(
    camera_identity_name: str | None,
    camera_identity_state: CameraIdentityState,
) -> str | None:
    """Require a live recognized face before loading per-user memory context."""
    if camera_identity_state != "recognized":
        return None
    if not camera_identity_name:
        return None
    cleaned = str(camera_identity_name).strip()
    if not cleaned or cleaned.lower() in {"unknown", "face"}:
        return None
    return cleaned


def _recap_context_from_recent_turns(
    recent_history: list[tuple[str, str]],
    *,
    focus_topic: str | None = None,
) -> str | None:
    """Build recap context from latest non-recap turns (up to 5)."""
    from llm_service import recap_turn_matches_topic

    eligible: list[tuple[str, str]] = []
    for user_text, assistant_text in recent_history:
        heard = str(user_text or "").strip()
        replied = str(assistant_text or "").strip()
        if not heard:
            continue
        # For topic-focused recap, keep prior recap turns if they still contain
        # substantive assistant content about that topic (e.g. earlier speaker discussion).
        if is_conversation_recap_question(heard) and not focus_topic:
            continue
        if focus_topic and not recap_turn_matches_topic(focus_topic, heard, replied):
            continue
        eligible.append((heard, replied))

    if focus_topic and not eligible:
        return None

    lines: list[str] = []
    latest_first = list(reversed(eligible[-5:]))
    for idx, (heard, replied) in enumerate(latest_first, start=1):
        lines.append(
            f"- Latest turn {idx}: User said: {heard[:180]} | Assistant replied: {replied[:180]}"
        )
    if not lines:
        return None
    header = (
        f"Recent turns about '{focus_topic}' (newest to oldest):\n"
        if focus_topic
        else "Recent turns (ordered newest to oldest, use these for recap):\n"
    )
    return header + "\n".join(lines)


def is_identity_question(user_text: str) -> bool:
    text = user_text.strip()
    if not text:
        return False
    return any(p.search(text) for p in _IDENTITY_QUESTION_PATTERNS)


_COMPOUND_FILLER = re.compile(
    r"\b(?:and|also|plus|please|hey|hi|hello|so|um|uh)\b",
    re.IGNORECASE,
)


def _extra_words_after_intent(pattern: re.Pattern[str], user_text: str) -> list[str]:
    leftover = pattern.sub(" ", user_text.strip())
    leftover = _COMPOUND_FILLER.sub(" ", leftover)
    leftover = re.sub(r"[^\w']+", " ", leftover)
    return [w for w in leftover.lower().split() if w]


def is_exclusive_intent(pattern: re.Pattern[str], user_text: str) -> bool:
    """True when the utterance is only this shortcut, not 'X and Y'."""
    text = user_text.strip()
    if not text or not pattern.search(text):
        return False
    return len(_extra_words_after_intent(pattern, text)) < 2


def is_local_time_question(user_text: str) -> bool:
    return bool(_LOCAL_TIME_QUESTION_PATTERN.search(user_text.strip()))


def is_exclusive_local_time_question(user_text: str) -> bool:
    return is_exclusive_intent(_LOCAL_TIME_QUESTION_PATTERN, user_text)


def non_time_question_text(user_text: str) -> str | None:
    """If they asked something AND the time, return the non-time question."""
    text = user_text.strip()
    if not is_local_time_question(text) or is_exclusive_local_time_question(text):
        return None
    leftover = _LOCAL_TIME_QUESTION_PATTERN.sub(" ", text)
    leftover = re.sub(r"\s+", " ", leftover).strip(" ,.;:!?")
    leftover = re.sub(
        r"^(?:and|also|plus)\s+|\s+(?:and|also|plus)$",
        "",
        leftover,
        flags=re.IGNORECASE,
    ).strip(" ,.;:!?")
    if len(leftover.split()) < 2:
        return None
    if leftover[-1] not in ".!?":
        leftover += "?"
    return leftover


def is_weather_question(user_text: str) -> bool:
    return bool(_WEATHER_QUESTION_PATTERN.search(user_text.strip()))


def is_exclusive_weather_question(user_text: str) -> bool:
    return is_exclusive_intent(_WEATHER_QUESTION_PATTERN, user_text)


def is_football_question(user_text: str) -> bool:
    return bool(_FOOTBALL_QUESTION_PATTERN.search(user_text.strip()))


def is_football_joke_request(user_text: str) -> bool:
    """Return whether the user has asked for a football joke."""
    text = user_text.strip()
    return bool(text) and bool(_FOOTBALL_JOKE_PATTERN.search(text)) and bool(
        re.search(r"\b(?:football|soccer)\b", text, re.IGNORECASE)
    )


def is_joke_request(user_text: str) -> bool:
    """Return whether the user asked for a general joke (not football-specific)."""
    text = user_text.strip()
    if not text:
        return False
    if is_football_joke_request(text):
        return False
    if any(p.search(text) for p in _JOKE_NEGATIVE_PATTERNS):
        return False
    return any(p.search(text) for p in _JOKE_REQUEST_PATTERNS)


def _draw_from_deck(deck: list[str], source: tuple[str, ...]) -> str:
    """Pop one shuffled entry, refilling the deck when it runs out."""
    if not deck:
        deck.extend(source)
        random.shuffle(deck)
    return deck.pop()


def random_joke_opener() -> str:
    """Return an excited lead-in, cycling a shuffled deck to avoid repeats."""
    return _draw_from_deck(_joke_opener_deck, JOKE_OPENERS)


def random_joke_closer() -> str:
    """Return a joyful sign-off so the punchline is clearly a laugh cue."""
    return _draw_from_deck(_joke_closer_deck, JOKE_CLOSERS)


def random_joke_reply() -> str:
    """Return a randomized joke, cycling a shuffled deck to avoid repeats."""
    global _joke_deck, _last_joke
    if not _joke_deck:
        _joke_deck = list(JOKES)
        random.shuffle(_joke_deck)
        if _last_joke and len(_joke_deck) > 1 and _joke_deck[-1] == _last_joke:
            _joke_deck[0], _joke_deck[-1] = _joke_deck[-1], _joke_deck[0]
    joke = _joke_deck.pop()
    _last_joke = joke
    return f"{random_joke_opener()} {joke} {random_joke_closer()}"


def is_world_cup_favourite_question(user_text: str) -> bool:
    """Return whether the user asks who NiNO supports in the World Cup."""
    return bool(_WORLD_CUP_FAVOURITE_PATTERN.search(user_text.strip()))


def is_live_football_question(user_text: str) -> bool:
    text = user_text.strip()
    return is_football_question(text) and bool(
        re.search(
            r"\b(?:live|right now|currently|playing now|who(?:'s| is) winning|"
            r"live score|scores now)\b",
            text,
            re.IGNORECASE,
        )
    )


def fifa_world_cup_winner_year(user_text: str) -> int | None:
    """Return the requested year for explicit FIFA World Cup winner questions."""
    text = user_text.strip()
    if not text or not re.search(r"\b(?:fifa|world cup)\b", text, re.IGNORECASE):
        return None
    if not re.search(r"\b(?:who won|winner|champion|champions)\b", text, re.IGNORECASE):
        return None
    match = re.search(r"\b((?:19|20)\d{2})\b", text)
    return int(match.group(1)) if match else None


def fifa_world_cup_top_scorer_year(user_text: str) -> int | None:
    """Return the requested year for FIFA World Cup top-scorer questions."""
    text = user_text.strip()
    if not text or not re.search(r"\b(?:fifa|world cup)\b", text, re.IGNORECASE):
        return None
    if not re.search(
        r"\b(?:top scorer|most goals|highest points|highest scorer|golden boot|"
        r"scored the most)\b",
        text,
        re.IGNORECASE,
    ):
        return None
    match = re.search(r"\b((?:19|20)\d{2})\b", text)
    return int(match.group(1)) if match else None


def world_cup_favourite_reply() -> str:
    """State NiNO's favourite World Cup team without requesting live scores."""
    return f"My favourite is {NINO_FAVOURITE_WORLD_CUP_TEAM}."


def local_server_time_reply() -> str:
    """Speak the server's configured local wall-clock time, not an LLM guess."""
    now = datetime.now().astimezone()
    hour = now.strftime("%I").lstrip("0") or "0"
    return f"It is {hour}:{now.strftime('%M %p')}, {now.strftime('%A, %B')} {now.day}."


def is_time_of_day_greeting(user_text: str) -> bool:
    """True for short greetings like 'good morning' / 'hi' (not questions)."""
    text = str(user_text or "").strip()
    if not text or len(text) > 48:
        return False
    if "?" in text:
        return False
    return bool(_TIME_OF_DAY_GREETING_RE.match(text))


def time_of_day_greeting_reply(viewer_name: str | None = None) -> str:
    """Greet using the current local period — never echo a wrong 'good morning'."""
    from alarm_time import day_part_greeting

    greeting = day_part_greeting()
    name = (viewer_name or "").strip()
    if name:
        return f"{greeting} {name}! How are you today?"
    return f"{greeting}! How are you today?"


_HOWAREYOU_RE = re.compile(
    r"^\s*(?:hey[, ]+|hi[, ]+|hello[, ]+)?"
    r"(?:how(?:'s| is| are) (?:it going|everything|things|your day|you(?: doing| feeling)?)|"
    r"how are ya|how're you(?: doing)?)"
    r"[.!?…]*\s*$",
    re.IGNORECASE,
)

_WELLBEING_STATUS_RE = re.compile(
    r"^\s*(?:i(?:'m| am)|we(?:'re| are)|doing|feeling)?\s*"
    r"(?:great|good|fine|well|okay|ok|awesome|amazing|fantastic|excellent|"
    r"not bad|pretty good|really good|so[- ]so|alright|all right|"
    r"tired|busy|happy|excited)"
    r"(?:\s*,?\s*(?:thanks|thank you|too))?"
    r"[.!?…]*\s*$",
    re.IGNORECASE,
)

_HOWAREYOU_REPLIES = (
    "I'm doing well, thanks! How about you?",
    "Pretty good over here! How's your day going?",
    "I'm good — glad you asked. How are you feeling?",
)

_WELLBEING_STATUS_REPLIES = (
    "That's lovely to hear! What's on your mind?",
    "Glad you're doing well! Want to chat about something?",
    "Awesome — I like that energy. What shall we talk about?",
    "Nice! Anything you'd like to dig into together?",
)


def is_howareyou_question(user_text: str) -> bool:
    text = str(user_text or "").strip()
    if not text or len(text) > 64:
        return False
    return bool(_HOWAREYOU_RE.match(text))


def is_wellbeing_status_reply(user_text: str) -> bool:
    """Short mood replies like 'I am great' after NiNO asked how they are."""
    text = str(user_text or "").strip()
    if not text or len(text) > 48 or "?" in text:
        return False
    return bool(_WELLBEING_STATUS_RE.match(text))


def howareyou_reply(viewer_name: str | None = None) -> str:
    name = (viewer_name or "").strip()
    base = random.choice(_HOWAREYOU_REPLIES)
    if name and random.random() < 0.45:
        return f"Hey {name}! {base}"
    return base


def wellbeing_status_reply(viewer_name: str | None = None) -> str:
    """Warm acknowledgement — keep chatting, never 'how can I assist'."""
    name = (viewer_name or "").strip()
    base = random.choice(_WELLBEING_STATUS_REPLIES)
    if name and random.random() < 0.35:
        return f"Good to hear, {name}! {base.split('! ', 1)[-1]}"
    return base


def is_servo_360_command(user_text: str) -> bool:
    text = user_text.strip()
    if not text:
        return False
    return any(p.search(text) for p in _SERVO_360_PATTERNS)


def esp_servo_360_url(device_id: str | None = None) -> str | None:
    from esp_playback import device_base_url

    base = device_base_url(device_id)
    if not base:
        return None
    return f"{base}/servo/360"


def esp_speaker_volume_url(device_id: str | None = None) -> str | None:
    from esp_playback import device_base_url

    base = device_base_url(device_id)
    if not base:
        return None
    return f"{base}/speaker/volume"


def trigger_esp_servo_360(device_id: str | None = None) -> tuple[bool, str | None]:
    """POST /servo/360 on the ESP. Returns (ok, error_code)."""
    url = esp_servo_360_url(device_id)
    if not url:
        return False, "no_esp_url"
    try:
        resp = requests.post(url, timeout=8)
        if resp.status_code == 200:
            return True, None
        try:
            payload = resp.json()
            err = str(payload.get("error", "request_failed"))
        except Exception:
            err = f"http_{resp.status_code}"
        logger.warning("ESP servo 360 failed: %s %s", resp.status_code, err)
        return False, err
    except requests.RequestException as exc:
        logger.warning("ESP servo 360 request failed: %s", exc)
        return False, "request_failed"


def get_esp_speaker_volume(device_id: str | None = None) -> tuple[int | None, str | None]:
    url = esp_speaker_volume_url(device_id)
    if not url:
        return None, "no_esp_url"
    try:
        resp = requests.get(url, timeout=8)
        if resp.status_code != 200:
            return None, f"http_{resp.status_code}"
        payload = resp.json()
        vol = int(payload.get("volume_percent", -1))
        if 0 <= vol <= 100:
            return vol, None
        return None, "bad_response"
    except Exception as exc:
        logger.warning("ESP speaker volume read failed: %s", exc)
        return None, "request_failed"


def set_esp_speaker_volume(
    percent: int, device_id: str | None = None
) -> tuple[int | None, str | None]:
    url = esp_speaker_volume_url(device_id)
    if not url:
        return None, "no_esp_url"
    pct = max(0, min(100, int(percent)))
    try:
        resp = requests.post(url, params={"value": str(pct)}, timeout=8)
        if resp.status_code != 200:
            return None, f"http_{resp.status_code}"
        payload = resp.json()
        applied = int(payload.get("volume_percent", pct))
        if 0 <= applied <= 100:
            return applied, None
        return None, "bad_response"
    except Exception as exc:
        logger.warning("ESP speaker volume set failed: %s", exc)
        return None, "request_failed"


def reply_for_servo_360_command(*, error: str | None = None) -> str:
    """Fixed spoken reply for servo 360 voice commands — no LLM."""
    if error == "no_esp_url":
        return (
            "I cannot reach the robot. "
            "Set ESP play WAV URL on the server to the board IP."
        )
    if error == "servos_not_ready":
        return "The servos are not ready. Connect the U2D2 on the USB hub and power the motors."
    if error == "already_running":
        return "A spin is already running."
    if error == "request_failed":
        return "I tried to start the spin but the robot did not respond."
    return "OK, doing the spin now."


def _parse_volume_value_phrase(raw: str) -> int | None:
    text = raw.strip().lower().replace("-", " ")
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\bpercent\b", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None
    if text in {"max", "maximum", "full"}:
        return 100
    if text in {"min", "minimum", "mute"}:
        return 0
    if text.isdigit():
        value = int(text)
        return value if 0 <= value <= 100 else None
    if text in _NUMBER_WORDS:
        return _NUMBER_WORDS[text]
    if text in {"one hundred", "a hundred"}:
        return 100
    if "hundred" in text:
        return 100
    return None


def parse_volume_command(user_text: str) -> tuple[str, int | None] | None:
    """
    Returns:
      ("set", target_percent)
      ("delta", delta_percent)
    """
    text = user_text.strip().lower()
    if not text or "volume" not in text:
        return None

    set_match = _VOLUME_SET_PATTERN.search(text)
    if set_match:
        value = _parse_volume_value_phrase(set_match.group(1))
        if value is not None:
            return ("set", value)
        return None

    step_match = _VOLUME_STEP_PATTERN.search(text)
    if not step_match:
        return None

    if step_match.group(1):
        action = step_match.group(1).lower()
        amount_raw = step_match.group(2) or ""
        sign = 1 if action in {"increase", "raise"} else -1
    else:
        direction = (step_match.group(3) or "").lower()
        amount_raw = step_match.group(4) or ""
        sign = 1 if direction == "up" else -1

    if not amount_raw.strip():
        return ("delta", sign * 10)
    amount = _parse_volume_value_phrase(amount_raw)
    if amount is None:
        return ("delta", sign * 10)
    return ("delta", sign * amount)


def apply_volume_command(
    user_text: str, *, device_id: str | None = None
) -> tuple[bool, str]:
    parsed = parse_volume_command(user_text)
    if parsed is None:
        return False, ""

    mode, value = parsed
    if mode == "set":
        applied, err = set_esp_speaker_volume(value or 0, device_id=device_id)
        if err:
            if err == "no_esp_url":
                return True, "I cannot reach the robot speaker right now."
            return True, "I could not change the volume on the robot."
        return True, f"Okay, speaker volume set to {applied} percent."

    current, err = get_esp_speaker_volume(device_id=device_id)
    if err or current is None:
        if err == "no_esp_url":
            return True, "I cannot reach the robot speaker right now."
        return True, "I could not read the current speaker volume."
    target = max(0, min(100, current + (value or 0)))
    applied, err = set_esp_speaker_volume(target, device_id=device_id)
    if err:
        return True, "I could not change the volume on the robot."
    return True, f"Okay, speaker volume set to {applied} percent."


def is_face_track_command(user_text: str) -> bool:
    return parse_face_track_command(user_text) is not None


def parse_face_track_command(user_text: str) -> bool | None:
    """True = enable tracking, False = disable, None = not a track command."""
    text = (user_text or "").strip()
    if not text:
        return None
    if any(p.search(text) for p in _FACE_TRACK_OFF_PATTERNS):
        return False
    if any(p.search(text) for p in _FACE_TRACK_ON_PATTERNS):
        return True
    return None


def esp_face_track_url(device_id: str | None = None) -> str | None:
    from esp_playback import device_base_url

    base = device_base_url(device_id)
    if not base:
        return None
    return f"{base.rstrip('/')}/face/track"


def set_esp_face_track(
    enabled: bool, device_id: str | None = None
) -> tuple[bool, str | None]:
    """POST /face/track on the ESP. Returns (ok, error_code)."""
    url = esp_face_track_url(device_id)
    if not url:
        return False, "no_esp_url"
    try:
        resp = requests.post(url, json={"enabled": bool(enabled)}, timeout=8)
        if resp.status_code == 200:
            return True, None
        try:
            payload = resp.json()
            err = str(payload.get("error", "request_failed"))
        except Exception:
            err = f"http_{resp.status_code}"
        logger.warning("ESP face track failed: %s %s", resp.status_code, err)
        return False, err
    except requests.RequestException as exc:
        logger.warning("ESP face track request failed: %s", exc)
        return False, "request_failed"


def apply_face_track_command(
    user_text: str, *, device_id: str | None = None
) -> tuple[bool, str]:
    parsed = parse_face_track_command(user_text)
    if parsed is None:
        return False, ""
    ok, err = set_esp_face_track(parsed, device_id=device_id)
    if not ok:
        # Stream sessions still apply face_track on the WS reply JSON.
        logger.warning(
            "HTTP face track %s — WS reply will still %s tracking",
            err or "failed",
            "enable" if parsed else "disable",
        )
    if parsed:
        return True, "I'll keep looking at you."
    return True, "I'll stop tracking your face."


def is_what_do_you_see_command(user_text: str) -> bool:
    text = (user_text or "").strip()
    if not text:
        return False
    return any(p.search(text) for p in _WHAT_DO_YOU_SEE_PATTERNS)


_visible_scene_snapshot: Callable[[str | None], tuple[list[str], list[dict]]] | None = (
    None
)


def configure_visible_scene_snapshot(
    fn: Callable[[str | None], tuple[list[str], list[dict]]] | None,
) -> None:
    """Live overlay names + YOLO detections for 'what do you see' / look-scan."""
    global _visible_scene_snapshot
    _visible_scene_snapshot = fn


def snapshot_visible_scene(
    device_id: str | None = None,
) -> tuple[list[str], list[dict]]:
    if _visible_scene_snapshot is None:
        return [], []
    try:
        names, detections = _visible_scene_snapshot(device_id)
        return list(names or []), list(detections or [])
    except Exception:
        logger.exception("visible scene snapshot failed device=%s", device_id)
        return [], []


def synthesize_look_scan_wav(
    reply: str,
    *,
    session_id: str = "",
    device_id: str = "",
) -> tuple[bytes, VoiceReplyMeta]:
    """Side-pose TTS during a look-scan. Does not retrigger look_scan."""
    meta = VoiceReplyMeta(session_id=session_id, device_id=device_id)
    meta.end_session = False
    meta.prompt_medical_ack = True
    meta.look_scan = False
    wav, _voice = synthesize_sapi_wav_bytes(reply)
    wav_out = resample_wav_bytes_to_mono_16bit(wav, VOICE_ASSIST_PLAYBACK_HZ)
    meta.timings = {
        "heard": "",
        "reply_text": reply[:200],
        "reply_path": "look_scan",
        "audio_out_seconds": round(_wav_seconds(wav_out), 2),
        "audio_out_bytes": len(wav_out),
    }
    return wav_out, meta


def _ensure_whisper() -> Any:
    global _WHISPER_MODEL
    if _WHISPER_MODEL is not None:
        return _WHISPER_MODEL
    from faster_whisper import WhisperModel

    device = resolve_whisper_device()
    compute_type = resolve_whisper_compute_type(device)
    device_index = max(0, SETTINGS.whisper_device_index)
    logger.info(
        "Loading faster-whisper model=%s device=%s index=%s compute_type=%s",
        SETTINGS.whisper_model,
        device,
        device_index,
        compute_type,
    )
    try:
        _WHISPER_MODEL = WhisperModel(
            SETTINGS.whisper_model,
            device=device,
            device_index=device_index if device == "cuda" else 0,
            compute_type=compute_type,
        )
    except Exception as exc:
        if device == "cpu":
            raise
        logger.warning(
            "GPU Whisper failed (%s); falling back to CPU int8.",
            exc,
        )
        device = "cpu"
        compute_type = "int8"
        _WHISPER_MODEL = WhisperModel(
            SETTINGS.whisper_model,
            device="cpu",
            compute_type=compute_type,
        )
    SETTINGS.whisper_runtime_device = device
    SETTINGS.whisper_runtime_compute_type = compute_type
    return _WHISPER_MODEL


def _wav_bytes_to_float_mono(wav_bytes: bytes) -> np.ndarray:
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        nch = wf.getnchannels()
        sw = wf.getsampwidth()
        sr = wf.getframerate()
        nframes = wf.getnframes()
        if nframes <= 0:
            raise RuntimeError("Input WAV has no audio frames.")
        raw = wf.readframes(nframes)
    if sw != 2:
        raise RuntimeError("Expected 16-bit PCM WAV from device.")
    pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if nch == 2:
        pcm = pcm.reshape(-1, 2).mean(axis=1)
    elif nch != 1:
        raise RuntimeError("Expected mono or stereo WAV.")
    # Whisper expects 16 kHz internally; faster-whisper resamples if needed
    if sr != 16000:
        pcm = _resample_linear(pcm, sr, 16000)
    return pcm


def _resample_linear(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    if src_rate == dst_rate:
        return samples
    duration = len(samples) / float(src_rate)
    target_n = int(duration * dst_rate)
    if target_n <= 0:
        return samples
    x_old = np.linspace(0.0, 1.0, num=len(samples), endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=target_n, endpoint=False)
    return np.interp(x_new, x_old, samples).astype(np.float32)


def _transcribe_whisper(wav_bytes: bytes, *, vad_filter: bool | None = None) -> str:
    model = _ensure_whisper()
    audio = _wav_bytes_to_float_mono(wav_bytes)
    if vad_filter is None:
        raw = os.environ.get("WHISPER_VAD_FILTER", "1").strip().lower()
        vad_filter = raw not in {"0", "false", "no", "off"}
    segments, _ = model.transcribe(
        audio,
        language=SETTINGS.whisper_language,
        beam_size=1,
        vad_filter=vad_filter,
        vad_parameters={"min_silence_duration_ms": 250},
        condition_on_previous_text=False,
        log_progress=False,
    )
    text = " ".join(seg.text.strip() for seg in segments).strip()
    if not text:
        logger.warning("Whisper returned no speech (audio %.2fs).", len(audio) / 16000.0)
    return text


def _transcribe_openai_whisper(wav_bytes: bytes) -> str:
    """OpenAI-compatible Whisper STT (OpenAI whisper-1, Groq whisper-large-v3, etc.)."""
    api_key = SETTINGS.openai_api_key
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set.")
    data: dict[str, str] = {
        "model": SETTINGS.openai_whisper_model,
        "response_format": "json",
    }
    if SETTINGS.whisper_language:
        data["language"] = SETTINGS.whisper_language
    url = f"{SETTINGS.openai_api_base}/audio/transcriptions"
    t0 = time.perf_counter()
    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {api_key}"},
        data=data,
        files={"file": ("voice.wav", wav_bytes, "audio/wav")},
        timeout=45,
    )
    log_http(
        "CLOUD",
        "POST",
        url,
        status=resp.status_code,
        stage_s=time.perf_counter() - t0,
        service="openai_whisper",
        model=SETTINGS.openai_whisper_model,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"OpenAI Whisper STT HTTP {resp.status_code}: {resp.text[:200]}"
        )
    payload = resp.json()
    text = str(payload.get("text", "")).strip()
    if not text:
        logger.warning("OpenAI Whisper STT returned no speech.")
    return text


def _transcribe_elevenlabs(wav_bytes: bytes) -> str:
    """ElevenLabs Scribe speech-to-text API."""
    api_key = SETTINGS.elevenlabs_api_key
    if not api_key:
        raise RuntimeError("ELEVENLABS_API_KEY is not set.")
    data: dict[str, str] = {
        "model_id": SETTINGS.elevenlabs_stt_model,
        "tag_audio_events": "false",
    }
    if SETTINGS.whisper_language:
        data["language_code"] = SETTINGS.whisper_language
    t0 = time.perf_counter()
    resp = requests.post(
        "https://api.elevenlabs.io/v1/speech-to-text",
        headers={"xi-api-key": api_key},
        data=data,
        files={"file": ("voice.wav", wav_bytes, "audio/wav")},
        timeout=30,
    )
    log_http(
        "CLOUD",
        "POST",
        "https://api.elevenlabs.io/v1/speech-to-text",
        status=resp.status_code,
        stage_s=time.perf_counter() - t0,
        service="elevenlabs_stt",
        model=SETTINGS.elevenlabs_stt_model,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"ElevenLabs STT HTTP {resp.status_code}: {resp.text[:200]}"
        )
    text = str(resp.json().get("text", "")).strip()
    if not text:
        logger.warning("ElevenLabs STT returned no speech.")
    return text


def transcribe_wav(
    wav_bytes: bytes, *, vad_filter: bool | None = None
) -> tuple[str, str]:
    """Transcribe device WAV. Returns (text, engine_used)."""
    audio_s = _wav_seconds(wav_bytes)
    pipeline_log(
        "ASR",
        "START",
        provider=SETTINGS.stt_provider,
        audio_s=f"{audio_s:.2f}",
        bytes=len(wav_bytes),
    )
    t0 = time.perf_counter()
    engine = SETTINGS.stt_provider
    try:
        if SETTINGS.stt_provider in {"openai_whisper", "openai", "whisper_api"}:
            try:
                text = _transcribe_openai_whisper(wav_bytes)
                engine = "openai_whisper"
                pipeline_log(
                    "ASR",
                    "DONE",
                    engine=engine,
                    text=text or "(empty)",
                    stage_s=time.perf_counter() - t0,
                )
                return text, engine
            except Exception as exc:
                pipeline_log(
                    "ASR",
                    "FAIL",
                    engine="openai_whisper",
                    error=str(exc),
                    stage_s=time.perf_counter() - t0,
                )
                logger.warning(
                    "OpenAI Whisper STT failed (%s); falling back to local Whisper.", exc
                )
        if SETTINGS.stt_provider == "elevenlabs":
            try:
                text = _transcribe_elevenlabs(wav_bytes)
                engine = "elevenlabs"
                pipeline_log(
                    "ASR",
                    "DONE",
                    engine=engine,
                    text=text or "(empty)",
                    stage_s=time.perf_counter() - t0,
                )
                return text, engine
            except Exception as exc:
                pipeline_log(
                    "ASR",
                    "FAIL",
                    engine="elevenlabs",
                    error=str(exc),
                    stage_s=time.perf_counter() - t0,
                )
                logger.warning("ElevenLabs STT failed (%s); falling back to Whisper.", exc)
        text = _transcribe_whisper(wav_bytes, vad_filter=vad_filter)
        engine = "whisper"
        pipeline_log(
            "ASR",
            "DONE",
            engine=engine,
            text=text or "(empty)",
            stage_s=time.perf_counter() - t0,
        )
        return text, engine
    except Exception as exc:
        pipeline_log(
            "ASR",
            "FAIL",
            engine=engine,
            error=str(exc),
            stage_s=time.perf_counter() - t0,
        )
        raise


def _speak_short_reply(
    meta: VoiceReplyMeta,
    *,
    reply: str,
    reply_path: str,
    heard: str,
    audio_input_format: str,
    audio_in_seconds: float,
    wav_bytes: bytes,
    stt_engine: str,
    t_start: float,
    t_stt: float,
    extra: dict[str, Any] | None = None,
) -> tuple[bytes, VoiceReplyMeta]:
    if extra:
        meta.timings.update(extra)
    _mark_continue_listen(meta, reply_path, heard)
    t_reply = time.perf_counter()
    wav, _voice = synthesize_sapi_wav_bytes(reply)
    tts_info = last_tts_synthesis_info()
    wav_out = resample_wav_bytes_to_mono_16bit(wav, VOICE_ASSIST_PLAYBACK_HZ)
    t_tts = time.perf_counter()
    audio_out_seconds = _wav_seconds(wav_out)
    meta.timings = {
        "heard": heard[:200],
        "reply_text": reply[:200],
        "reply_path": reply_path,
        "audio_input_format": audio_input_format,
        "audio_in_seconds": round(audio_in_seconds, 2),
        "audio_in_bytes": len(wav_bytes),
        "audio_out_seconds": round(audio_out_seconds, 2),
        "audio_out_bytes": len(wav_out),
        "stt_engine": stt_engine,
        "tts_provider": tts_info["provider"],
        "tts_voice": tts_info["voice"],
        "tts_style": tts_info.get("style", ""),
        "stt_seconds": round(t_stt - t_start, 3),
        "reply_seconds": round(t_reply - t_stt, 3),
        "tts_seconds": round(t_tts - t_reply, 3),
        "process_total_seconds": round(t_tts - t_start, 3),
        "continue_listen": bool(meta.prompt_medical_ack),
    }
    if extra:
        meta.timings.update(extra)
    log_nino_voice(
        "SEND",
        turn=meta.timings.get("turn"),
        path=reply_path,
        continue_listen=int(bool(meta.prompt_medical_ack)),
        heard=heard[:120] or "(empty)",
        reply=reply[:120],
        stt=meta.timings.get("stt_seconds"),
        tts=meta.timings.get("tts_seconds"),
    )
    return wav_out, meta


def _silent_close(
    meta: VoiceReplyMeta,
    *,
    reply_path: str,
    heard: str,
    audio_input_format: str,
    audio_in_seconds: float,
    wav_bytes: bytes,
    stt_engine: str,
    t_start: float,
    t_stt: float,
    extra: dict[str, Any] | None = None,
    preserve_continue_listen: bool = False,
) -> tuple[bytes, VoiceReplyMeta]:
    """Close the mic with no spoken reply — stops the sorry / listen loop."""
    keep_open = bool(preserve_continue_listen)
    meta.prompt_medical_ack = keep_open
    t_done = time.perf_counter()
    wav_out = minimal_voice_reply_wav()
    meta.timings = {
        "heard": str(heard or "")[:200],
        "reply_text": "",
        "reply_path": reply_path,
        "audio_input_format": audio_input_format,
        "audio_in_seconds": round(audio_in_seconds, 2),
        "audio_in_bytes": len(wav_bytes),
        "audio_out_seconds": round(_wav_seconds(wav_out), 2),
        "audio_out_bytes": len(wav_out),
        "stt_engine": stt_engine,
        "tts_provider": "none",
        "tts_voice": "",
        "stt_seconds": round(max(0.0, t_stt - t_start), 3),
        "reply_seconds": 0.0,
        "tts_seconds": 0.0,
        "process_total_seconds": round(t_done - t_start, 3),
        "continue_listen": keep_open,
        "wake_ok": False,
    }
    if extra:
        meta.timings.update(extra)
    log_nino_voice(
        "SKIP",
        turn=meta.timings.get("turn"),
        path=reply_path,
        heard=str(heard or "")[:120] or "(empty)",
        continue_listen=int(keep_open),
        next="mic stays open until goodbye" if keep_open else "waiting for a new Ok Nino",
    )
    return wav_out, meta


def _wake_gate_result(
    meta: VoiceReplyMeta,
    *,
    ok: bool,
    heard: str,
    phrase: str,
    audio_input_format: str,
    audio_in_seconds: float,
    wav_bytes: bytes,
    stt_engine: str,
    t_start: float,
    t_stt: float,
    extra: dict[str, Any] | None = None,
) -> tuple[bytes, VoiceReplyMeta]:
    """First idle-to-session clip: ASR must hear Ok Nino or Hello before GREET/LED."""
    path = "wake_ok" if ok else "wake_reject"
    meta.end_session = not ok
    meta.prompt_medical_ack = False
    t_done = time.perf_counter()
    wav_out = b"" if ok else minimal_voice_reply_wav()
    meta.timings = {
        "heard": str(heard or "")[:200],
        "reply_text": "",
        "reply_path": path,
        "audio_input_format": audio_input_format,
        "audio_in_seconds": round(audio_in_seconds, 2),
        "audio_in_bytes": len(wav_bytes),
        "audio_out_seconds": round(_wav_seconds(wav_out), 2) if wav_out else 0.0,
        "audio_out_bytes": len(wav_out),
        "stt_engine": stt_engine,
        "tts_provider": "none",
        "tts_voice": "",
        "stt_seconds": round(max(0.0, t_stt - t_start), 3),
        "reply_seconds": 0.0,
        "tts_seconds": 0.0,
        "process_total_seconds": round(t_done - t_start, 3),
        "continue_listen": ok,
        "wake_ok": ok,
        "wake_phrase": phrase,
    }
    if extra:
        meta.timings.update(extra)
    log_nino_voice(
        "WAKE",
        turn=meta.timings.get("turn"),
        ok=1 if ok else 0,
        phrase=phrase or "(none)",
        heard=str(heard or "")[:120] or "(empty)",
        next="GREET" if ok else "GPIO 5, ignore clip",
    )
    return wav_out, meta


def _short_handled_wav(
    meta: VoiceReplyMeta,
    *,
    reply: str,
    reply_path: str,
    user_text: str,
    audio_input_format: str,
    audio_in_seconds: float,
    wav_bytes: bytes,
    stt_engine: str,
    t_start: float,
    t_stt: float,
    look_scan: bool = False,
    motion: list[str] | None = None,
    face_track: bool | None = None,
) -> tuple[bytes, VoiceReplyMeta]:
    t_reply = time.perf_counter()
    wav, _voice = synthesize_sapi_wav_bytes(reply)
    tts_info = last_tts_synthesis_info()
    wav_out = resample_wav_bytes_to_mono_16bit(wav, VOICE_ASSIST_PLAYBACK_HZ)
    t_tts = time.perf_counter()
    audio_out_seconds = _wav_seconds(wav_out)
    if look_scan:
        meta.look_scan = True
        meta.motion = None
    elif motion:
        meta.motion = list(motion)
    if face_track is not None:
        meta.face_track = bool(face_track)
    _mark_continue_listen(meta, reply_path, user_text)
    meta.timings = {
        "heard": user_text[:200],
        "reply_text": reply[:200],
        "reply_path": reply_path,
        "audio_input_format": audio_input_format,
        "audio_in_seconds": round(audio_in_seconds, 2),
        "audio_in_bytes": len(wav_bytes),
        "audio_out_seconds": round(audio_out_seconds, 2),
        "audio_out_bytes": len(wav_out),
        "stt_engine": stt_engine,
        "tts_provider": tts_info["provider"],
        "tts_voice": tts_info["voice"],
        "tts_style": tts_info.get("style", ""),
        "stt_seconds": round(t_stt - t_start, 3),
        "reply_seconds": round(t_reply - t_stt, 3),
        "tts_seconds": round(t_tts - t_reply, 3),
        "process_total_seconds": round(t_tts - t_start, 3),
        "continue_listen": bool(meta.prompt_medical_ack),
        "look_scan": bool(meta.look_scan),
        "motion": meta.motion,
    }
    logger.info(
        "Latency | stt(%s)=%.2fs reply(%s)=%.2fs tts=%.2fs total=%.2fs | in=%.1fs out=%.1fs audio",
        stt_engine,
        meta.timings["stt_seconds"],
        reply_path,
        meta.timings["reply_seconds"],
        meta.timings["tts_seconds"],
        meta.timings["process_total_seconds"],
        audio_in_seconds,
        audio_out_seconds,
    )
    return wav_out, meta


def process_voice_wav(
    wav_bytes: bytes,
    viewer_name: str | None = None,
    *,
    camera_identity_name: str | None = None,
    camera_identity_state: CameraIdentityState = "no_face",
    camera_scene: str | None = None,
    visible_names: list[str] | None = None,
    device_id: str = "",
    session_kind: str = "wake",
    session_id: str = "",
    aux_energy: int | None = None,
    voice_turn: int | None = None,
    pipeline_t0: float | None = None,
) -> tuple[bytes, VoiceReplyMeta]:
    meta = VoiceReplyMeta(device_id=device_id or "")
    meta.session_id = str(session_id or "").strip()
    session = "continue" if str(session_kind or "").strip().lower() in {
        "continue",
        "conv",
        "followup",
        "ack",
    } else "wake"
    if not wav_bytes:
        raise RuntimeError("Empty audio.")
    if len(wav_bytes) > SETTINGS.max_request_bytes:
        t_start = time.perf_counter()
        logger.warning(
            "Audio exceeds size limit device=%s bytes=%s max=%s",
            device_id,
            len(wav_bytes),
            SETTINGS.max_request_bytes,
        )
        return _silent_close(
            meta,
            reply_path="too_long",
            heard="",
            audio_input_format="wav",
            audio_in_seconds=len(wav_bytes) / 32_000.0,
            wav_bytes=wav_bytes,
            stt_engine="skipped",
            t_start=t_start,
            t_stt=t_start,
        )

    try:
        wav_bytes, audio_input_format = normalize_voice_input_bytes(wav_bytes)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc

    t_start = time.perf_counter()
    begin_pipeline(
        device_id=device_id,
        turn=voice_turn,
        session=session,
        t0=pipeline_t0 or t_start,
    )
    audio_in_seconds = _wav_seconds(wav_bytes)
    peak_energy = clip_peak_energy(wav_bytes, aux_energy)
    mean_energy = wav_mean_frame_energy(wav_bytes)
    energy_gate = min_speech_energy()
    preserve_continue = _preserve_continue_on_skip(
        session=session,
        session_id=meta.session_id,
        device_id=device_id,
    )
    from voice_listen_state import in_post_tts_grace

    post_tts_grace = in_post_tts_grace(meta.session_id, device_id)
    meta.timings["turn"] = voice_turn
    log_nino_voice(
        "RECV",
        turn=voice_turn,
        session=session,
        device=device_id or "-",
        audio_s=f"{audio_in_seconds:.2f}",
        bytes=len(wav_bytes),
        format=audio_input_format,
        energy=peak_energy,
        mean_energy=mean_energy,
        energy_th=energy_gate,
    )

    if peak_energy < energy_gate:
        return _silent_close(
            meta,
            reply_path="stt_silent",
            heard="",
            audio_input_format=audio_input_format,
            audio_in_seconds=audio_in_seconds,
            wav_bytes=wav_bytes,
            stt_engine="skipped",
            t_start=t_start,
            t_stt=t_start,
            preserve_continue_listen=preserve_continue and post_tts_grace,
            extra={
                "session": session,
                "wake_ok": False,
                "energy": peak_energy,
                "mean_energy": mean_energy,
                "energy_th": energy_gate,
                "turn": voice_turn,
                "post_tts_grace": post_tts_grace,
            },
        )
    long_clip_s = long_clip_min_seconds()
    long_mean_th = long_clip_min_mean_energy()
    if (
        audio_in_seconds >= long_clip_s
        and mean_energy < long_mean_th
        and not post_tts_grace
        and not speech_like_clip(peak_energy, mean_energy)
    ):
        logger.info(
            "Voice clip rejected (long low-mean noise) | mean=%s peak=%s audio_s=%.2f th=%s",
            mean_energy,
            peak_energy,
            audio_in_seconds,
            long_mean_th,
        )
        return _silent_close(
            meta,
            reply_path="stt_silent",
            heard="",
            audio_input_format=audio_input_format,
            audio_in_seconds=audio_in_seconds,
            wav_bytes=wav_bytes,
            stt_engine="skipped",
            t_start=t_start,
            t_stt=t_start,
            preserve_continue_listen=preserve_continue,
            extra={
                "session": session,
                "wake_ok": False,
                "energy": peak_energy,
                "mean_energy": mean_energy,
                "energy_th": energy_gate,
                "turn": voice_turn,
                "post_tts_grace": post_tts_grace,
            },
        )
    quiet_clip_s = quiet_clip_min_seconds()
    quiet_mean_th = quiet_clip_min_mean_energy()
    if (
        audio_in_seconds >= quiet_clip_s
        and mean_energy < quiet_mean_th
        and not speech_like_clip(peak_energy, mean_energy)
    ):
        logger.info(
            "Voice clip rejected (quiet clip) | mean=%s peak=%s audio_s=%.2f",
            mean_energy,
            peak_energy,
            audio_in_seconds,
        )
        return _silent_close(
            meta,
            reply_path="stt_silent",
            heard="",
            audio_input_format=audio_input_format,
            audio_in_seconds=audio_in_seconds,
            wav_bytes=wav_bytes,
            stt_engine="skipped",
            t_start=t_start,
            t_stt=t_start,
            preserve_continue_listen=preserve_continue and post_tts_grace,
            extra={
                "session": session,
                "wake_ok": False,
                "energy": peak_energy,
                "mean_energy": mean_energy,
                "energy_th": energy_gate,
                "turn": voice_turn,
                "post_tts_grace": post_tts_grace,
            },
        )

    user_text, stt_engine = transcribe_wav(
        wav_bytes, vad_filter=False if session == "wake" else None
    )
    t_stt = time.perf_counter()
    reply_path = "llm"
    log_nino_voice(
        "STT",
        turn=voice_turn,
        engine=stt_engine,
        energy=peak_energy,
        text=user_text[:200] or "(empty)",
    )

    if not user_text.strip():
        if session == "wake":
            _record_rejected_wake(
                device_id=device_id,
                heard="",
                reason="STT empty",
                audio_s=audio_in_seconds,
            )
            return _wake_gate_result(
                meta,
                ok=False,
                heard="",
                phrase="",
                audio_input_format=audio_input_format,
                audio_in_seconds=audio_in_seconds,
                wav_bytes=wav_bytes,
                stt_engine=stt_engine,
                t_start=t_start,
                t_stt=t_stt,
                extra={"session": session, "energy": peak_energy, "turn": voice_turn},
            )
        return _silent_close(
            meta,
            reply_path="stt_empty",
            heard="",
            audio_input_format=audio_input_format,
            audio_in_seconds=audio_in_seconds,
            wav_bytes=wav_bytes,
            stt_engine=stt_engine,
            t_start=t_start,
            t_stt=t_stt,
            extra={
                "session": session,
                "wake_ok": False,
                "energy": peak_energy,
                "turn": voice_turn,
            },
        )

    # Idle → session: ASR must hear Ok Nino or a leading Hello. In-session turns do not.
    wake_found, command_text, wake_phrase = extract_wake_and_command(user_text)
    heard_raw = user_text
    if session == "wake":
        extra = {
            "session": session,
            "energy": peak_energy,
            "turn": voice_turn,
            "device": device_id or "-",
        }
        if not wake_found:
            _record_rejected_wake(
                device_id=device_id,
                heard=heard_raw,
                reason="no Ok Nino/Hello",
                audio_s=audio_in_seconds,
            )
            return _wake_gate_result(
                meta,
                ok=False,
                heard=heard_raw,
                phrase="",
                audio_input_format=audio_input_format,
                audio_in_seconds=audio_in_seconds,
                wav_bytes=wav_bytes,
                stt_engine=stt_engine,
                t_start=t_start,
                t_stt=t_stt,
                extra=extra,
            )
        return _wake_gate_result(
            meta,
            ok=True,
            heard=heard_raw,
            phrase=wake_phrase,
            audio_input_format=audio_input_format,
            audio_in_seconds=audio_in_seconds,
            wav_bytes=wav_bytes,
            stt_engine=stt_engine,
            t_start=t_start,
            t_stt=t_stt,
            extra=extra,
        )
    # In-session: strip a leading "Ok Nino" so the command remains. A bare
    # "Hello" is the user's greeting — keep it (do not treat as empty STT).
    if wake_found and command_text.strip():
        user_text = command_text
        log_nino_voice(
            "CONV",
            turn=voice_turn,
            ok=1,
            device=device_id or "-",
            phrase=wake_phrase,
            command=user_text[:200] or "(none)",
            session=session,
        )
    else:
        log_nino_voice(
            "CONV",
            turn=voice_turn,
            ok=1,
            device=device_id or "-",
            phrase=wake_phrase if wake_found else "(none)",
            command=user_text[:200],
            session=session,
        )
    if not user_text.strip():
        return _silent_close(
            meta,
            reply_path="stt_empty",
            heard=heard_raw,
            audio_input_format=audio_input_format,
            audio_in_seconds=audio_in_seconds,
            wav_bytes=wav_bytes,
            stt_engine=stt_engine,
            t_start=t_start,
            t_stt=t_stt,
            extra={
                "session": session,
                "wake_in_speech": wake_found,
                "turn": voice_turn,
            },
        )
    if is_bare_thank_you_stt(user_text):
        logger.info(
            "Voice STT skipped bare thank-you (aux/TTS artefact) | heard: %s",
            user_text[:80],
        )
        return _silent_close(
            meta,
            reply_path="stt_rejected",
            heard=user_text,
            audio_input_format=audio_input_format,
            audio_in_seconds=audio_in_seconds,
            wav_bytes=wav_bytes,
            stt_engine=stt_engine,
            t_start=t_start,
            t_stt=t_stt,
            preserve_continue_listen=preserve_continue,
            extra={"session": session, "energy": peak_energy, "turn": voice_turn},
        )
    log_nino_voice("CMD", turn=voice_turn, session=session, text=user_text[:200])

    from session_identity import get_session_identity

    ident = get_session_identity(device_id)
    if ident is not None and ident.is_active() and not ident.in_registration():
        names = [n for n in (visible_names or []) if str(n).strip()]
        if not names and camera_identity_state == "recognized" and camera_identity_name:
            names = [str(camera_identity_name).strip()]
        switched = ident.apply_visible_scene(
            visible_names=names,
            scene_state=camera_identity_state or "no_face",
            allow_register=False,
        )
        if switched is not None and switched.user_name:
            from conversation_sessions import bind_session_user

            if meta.session_id:
                bind_session_user(
                    meta.session_id,
                    device_id=device_id,
                    user_name=switched.user_name,
                )
            logger.info(
                "Voice identity refresh (no register) user=%s guest=%s heard: %s",
                switched.user_name,
                switched.is_guest,
                user_text[:80],
            )

    handled_volume, volume_reply = apply_volume_command(
        user_text, device_id=device_id or None
    )
    if handled_volume:
        reply_path = "volume"
        logger.info("Voice volume command | heard: %s", user_text[:120])
        reply = volume_reply
        t_reply = time.perf_counter()
        wav, _voice = synthesize_sapi_wav_bytes(reply)
        tts_info = last_tts_synthesis_info()
        wav_out = resample_wav_bytes_to_mono_16bit(wav, VOICE_ASSIST_PLAYBACK_HZ)
        t_tts = time.perf_counter()

        audio_out_seconds = _wav_seconds(wav_out)
        _mark_continue_listen(meta, reply_path, user_text)
        meta.timings = {
            "heard": user_text[:200],
            "reply_text": reply[:200],
            "reply_path": reply_path,
            "audio_input_format": audio_input_format,
            "audio_in_seconds": round(audio_in_seconds, 2),
            "audio_in_bytes": len(wav_bytes),
            "audio_out_seconds": round(audio_out_seconds, 2),
            "audio_out_bytes": len(wav_out),
            "stt_engine": stt_engine,
            "tts_provider": tts_info["provider"],
            "tts_voice": tts_info["voice"],
            "tts_style": tts_info.get("style", ""),
            "stt_seconds": round(t_stt - t_start, 3),
            "reply_seconds": round(t_reply - t_stt, 3),
            "tts_seconds": round(t_tts - t_reply, 3),
            "process_total_seconds": round(t_tts - t_start, 3),
            "continue_listen": bool(meta.prompt_medical_ack),
        }
        logger.info(
            "Latency | stt(%s)=%.2fs reply(%s)=%.2fs tts=%.2fs total=%.2fs | in=%.1fs out=%.1fs audio",
            stt_engine,
            meta.timings["stt_seconds"],
            reply_path,
            meta.timings["reply_seconds"],
            meta.timings["tts_seconds"],
            meta.timings["process_total_seconds"],
            audio_in_seconds,
            audio_out_seconds,
        )
        return wav_out, meta

    handled_track, track_reply = apply_face_track_command(
        user_text, device_id=device_id or None
    )
    if handled_track:
        logger.info("Voice face-track command | heard: %s", user_text[:120])
        return _short_handled_wav(
            meta,
            reply=track_reply,
            reply_path="face_track",
            user_text=user_text,
            audio_input_format=audio_input_format,
            audio_in_seconds=audio_in_seconds,
            wav_bytes=wav_bytes,
            stt_engine=stt_engine,
            t_start=t_start,
            t_stt=t_stt,
            face_track=parse_face_track_command(user_text),
        )

    from servo_tts_motion import parse_repeat_yes_no_command

    repeat_yn = parse_repeat_yes_no_command(user_text)
    if repeat_yn == "no":
        logger.info("Voice say-no-no-no | heard: %s", user_text[:120])
        return _short_handled_wav(
            meta,
            reply="No, no, no.",
            reply_path="say_no3",
            user_text=user_text,
            audio_input_format=audio_input_format,
            audio_in_seconds=audio_in_seconds,
            wav_bytes=wav_bytes,
            stt_engine=stt_engine,
            t_start=t_start,
            t_stt=t_stt,
            motion=["shake3"],
        )
    if repeat_yn == "yes":
        logger.info("Voice say-yes-yes-yes | heard: %s", user_text[:120])
        return _short_handled_wav(
            meta,
            reply="Yes, yes, yes.",
            reply_path="say_yes3",
            user_text=user_text,
            audio_input_format=audio_input_format,
            audio_in_seconds=audio_in_seconds,
            wav_bytes=wav_bytes,
            stt_engine=stt_engine,
            t_start=t_start,
            t_stt=t_stt,
            motion=["nod3"],
        )

    if is_what_do_you_see_command(user_text):
        from object_detection_service import spoken_scene_report

        names, detections = snapshot_visible_scene(device_id or None)
        if not names and visible_names:
            names = list(visible_names)
        reply = spoken_scene_report(names, detections, pose="center")
        logger.info(
            "Voice what-do-you-see | names=%s objects=%s heard: %s",
            names,
            [d.get("label") for d in detections[:8]],
            user_text[:120],
        )
        return _short_handled_wav(
            meta,
            reply=reply,
            reply_path="look_scan",
            user_text=user_text,
            audio_input_format=audio_input_format,
            audio_in_seconds=audio_in_seconds,
            wav_bytes=wav_bytes,
            stt_engine=stt_engine,
            t_start=t_start,
            t_stt=t_stt,
            look_scan=True,
        )

    ident_handled = False
    if ident is not None and ident.should_skip_prompt_echo(user_text):
        logger.info(
            "Voice session GREET echo skipped | heard: %s",
            user_text[:120],
        )
        return _silent_close(
            meta,
            reply_path="silent_skip",
            heard=user_text,
            audio_input_format=audio_input_format,
            audio_in_seconds=audio_in_seconds,
            wav_bytes=wav_bytes,
            stt_engine=stt_engine,
            t_start=t_start,
            t_stt=t_stt,
            preserve_continue_listen=preserve_continue,
            extra={"session": session, "energy": peak_energy, "turn": voice_turn},
        )
    if ident is not None and ident.in_registration():
        ident_result = ident.handle_voice(user_text)
        if ident_result.handled:
            ident_handled = True
            reply_path = "face_registration"
            reply = ident_result.reply
            if ident_result.registered_name:
                meta.registered_face_name = ident_result.registered_name
                from conversation_sessions import bind_session_user

                if meta.session_id:
                    bind_session_user(
                        meta.session_id,
                        device_id=device_id,
                        user_name=ident_result.registered_name,
                    )
            if ident_result.relisten_after_reply:
                meta.face_reg_relisten = True
            logger.info(
                "Voice session identity | registered=%s heard: %s",
                ident_result.registered_name or "(none)",
                user_text[:120],
            )
            if ident_result.relisten_after_reply and not reply:
                meta.timings = {
                    "heard": user_text[:200],
                    "reply_text": "(session identity relisten)",
                    "reply_path": reply_path,
                    "audio_input_format": audio_input_format,
                    "audio_in_seconds": round(audio_in_seconds, 2),
                    "audio_in_bytes": len(wav_bytes),
                    "stt_engine": stt_engine,
                    "stt_seconds": round(t_stt - t_start, 3),
                }
                return b"", meta

    if not ident_handled and (
        is_bare_thank_you_stt(user_text)
        or is_likely_tts_echo(user_text)
        or is_unintelligible_stt(user_text)
        or is_whisper_silence_hallucination(
            user_text,
            mean_energy=mean_energy,
            peak_energy=peak_energy,
            audio_seconds=audio_in_seconds,
        )
    ):
        logger.info("Voice STT rejected (echo/garbled) | heard: %s", user_text[:120])
        return _silent_close(
            meta,
            reply_path="stt_rejected",
            heard=user_text,
            audio_input_format=audio_input_format,
            audio_in_seconds=audio_in_seconds,
            wav_bytes=wav_bytes,
            stt_engine=stt_engine,
            t_start=t_start,
            t_stt=t_stt,
            preserve_continue_listen=preserve_continue,
            extra={"session": session, "energy": peak_energy, "turn": voice_turn},
        )

    from alarm_voice import handle_alarm_voice

    from memory_service import get_memory_service, resolve_alarm_user

    memory_svc = get_memory_service()
    memory_name = _live_memory_viewer_name(
        camera_identity_name,
        camera_identity_state,
    )
    preserve_identity = camera_identity_state == "recognized" or post_tts_grace
    if not memory_name and viewer_name and preserve_identity:
        # Continue-listen turns may lose the live face for a moment after TTS.
        cleaned_viewer = viewer_name.strip()
        if cleaned_viewer and cleaned_viewer.lower() not in {"unknown", "face"}:
            memory_name = cleaned_viewer
    ident_user = None
    ident_guest = False
    if ident is not None:
        ident_user, ident_guest = ident.current_user()
        if ident_user:
            if ident_guest or preserve_identity:
                memory_name = ident_user
                viewer_name = ident_user
            elif camera_identity_state == "no_face":
                switch = ident.apply_visible_scene(
                    visible_names=visible_names or [],
                    scene_state="no_face",
                    allow_register=False,
                )
                if switch is not None and switch.user_name:
                    memory_name = switch.user_name
                    viewer_name = switch.user_name
                    if switch.is_guest and meta.session_id:
                        from conversation_sessions import bind_session_user

                        bind_session_user(
                            meta.session_id,
                            device_id=device_id,
                            user_name=switch.user_name,
                        )
                    logger.info(
                        "Voice session identity: no live face — guest %s",
                        switch.user_name,
                    )
    memory_ctx = None
    if memory_name:
        memory_ctx = memory_svc.load_context(memory_name, query_text=user_text)
    alarm_user_id, alarm_person_name = resolve_alarm_user(
        camera_identity_name,
        camera_identity_state,
    )
    t_memory = time.perf_counter()

    # Music stop must beat goodbye / LLM: while a track is playing, "stop",
    # "shut up", and STT mashups like "shupupo" should only stop the song.
    if reply_path == "llm":
        from music_voice import handle_music_voice

        music_result = handle_music_voice(user_text, device_id=device_id)
        if music_result.handled:
            reply_path = music_result.reply_path
            reply = music_result.reply
            logger.info(
                "Voice music command | path=%s heard: %s",
                reply_path,
                user_text[:120],
            )

    if reply_path == "llm" and is_conversation_goodbye(user_text):
        reply_path = "goodbye"
        reply = conversation_goodbye_reply()
        logger.info("Voice conversation goodbye | heard: %s", user_text[:120])

    if reply_path == "llm" and is_time_of_day_greeting(user_text):
        reply_path = "greeting"
        greet_name = memory_name or (
            viewer_name.strip() if viewer_name else None
        )
        reply = time_of_day_greeting_reply(greet_name)
        logger.info(
            "Voice time-of-day greeting | reply=%s | heard: %s",
            reply[:80],
            user_text[:120],
        )

    if reply_path == "llm" and is_howareyou_question(user_text):
        reply_path = "smalltalk"
        reply = howareyou_reply(memory_name or viewer_name)
        logger.info("Voice how-are-you | heard: %s", user_text[:120])

    if reply_path == "llm" and is_wellbeing_status_reply(user_text):
        reply_path = "smalltalk"
        reply = wellbeing_status_reply(memory_name or viewer_name)
        logger.info("Voice wellbeing status | heard: %s", user_text[:120])

    if (
        reply_path == "llm"
        and is_joke_request(user_text)
        and is_local_time_question(user_text)
    ):
        reply_path = "joke_and_time"
        reply = f"{random_joke_reply()} {local_server_time_reply()}"
        logger.info("Voice joke + local-time query | heard: %s", user_text[:120])

    if reply_path == "llm" and is_exclusive_local_time_question(user_text):
        reply_path = "local_time"
        reply = local_server_time_reply()
        logger.info("Voice local-time query | heard: %s", user_text[:120])

    if reply_path == "llm" and is_exclusive_weather_question(user_text):
        from device_registry import get_device_registry
        from weather_service import (
            DeviceLocationUnavailableError,
            WeatherUnavailableError,
            get_weather_service,
            weather_voice_reply,
        )

        reply_path = "weather"
        from user_devices import normalize_device_mac

        registry = get_device_registry()
        mac = normalize_device_mac(device_id)
        device = registry.get(mac) if mac else registry.resolve_or_default(device_id or None)
        if device is None:
            reply = (
                "I do not have a location configured for that device. "
                "Please set its location first."
            )
        else:
            try:
                reply = weather_voice_reply(
                    device, get_weather_service().current_for_device(device)
                )
                logger.info(
                    "Voice weather query | device=%s heard: %s",
                    device.device_id,
                    user_text[:120],
                )
            except DeviceLocationUnavailableError:
                reply = (
                    f"I do not have a location configured for {device.display_name or device.device_id}. "
                    "Please set its location first."
                )
            except WeatherUnavailableError:
                reply = "I cannot retrieve the current weather right now. Please try again soon."

    top_scorer_year = fifa_world_cup_top_scorer_year(user_text)
    if reply_path == "llm" and top_scorer_year is not None:
        from football_service import (
            TournamentResultUnavailableError,
            get_fifa_tournament_service,
        )

        reply_path = "fifa_world_cup_top_scorer"
        try:
            player, goals = get_fifa_tournament_service().world_cup_top_scorer(
                top_scorer_year
            )
            reply = (
                f"{player} was the top scorer at the {top_scorer_year} FIFA World Cup "
                f"with {goals} goals."
            )
            logger.info(
                "Voice FIFA World Cup top-scorer query | year=%d heard: %s",
                top_scorer_year,
                user_text[:120],
            )
        except TournamentResultUnavailableError:
            reply = (
                f"I cannot confirm the top scorer at the {top_scorer_year} FIFA World "
                "Cup right now."
            )

    winner_year = fifa_world_cup_winner_year(user_text)
    if reply_path == "llm" and winner_year is not None:
        from football_service import (
            TournamentResultUnavailableError,
            get_fifa_tournament_service,
        )

        reply_path = "fifa_world_cup_winner"
        try:
            winner = get_fifa_tournament_service().world_cup_winner(winner_year)
            reply = f"{winner} won the {winner_year} FIFA World Cup."
            logger.info(
                "Voice FIFA World Cup winner query | year=%d heard: %s",
                winner_year,
                user_text[:120],
            )
        except TournamentResultUnavailableError:
            reply = (
                f"I cannot confirm the winner of the {winner_year} FIFA World Cup "
                "right now."
            )

    if reply_path == "llm" and is_world_cup_favourite_question(user_text):
        reply_path = "world_cup_favourite"
        reply = world_cup_favourite_reply()
        logger.info("Voice World Cup favourite query | heard: %s", user_text[:120])

    if reply_path == "llm" and is_football_joke_request(user_text):
        reply_path = "football_joke"
        reply = (
            f"{random_joke_opener()} I was going to tell you an offside joke, "
            f"but you probably wouldn't get it. {random_joke_closer()}"
        )
        logger.info("Voice football joke query | heard: %s", user_text[:120])

    if reply_path == "llm" and is_joke_request(user_text):
        reply_path = "joke"
        reply = random_joke_reply()
        logger.info("Voice joke query | heard: %s", user_text[:120])

    if reply_path == "llm" and is_live_football_question(user_text):
        from football_service import (
            FootballNotConfiguredError,
            FootballUnavailableError,
            get_football_service,
            live_football_voice_reply,
        )

        reply_path = "football_live"
        try:
            reply = live_football_voice_reply(get_football_service().live_matches())
            logger.info("Voice live-football query | heard: %s", user_text[:120])
        except FootballNotConfiguredError:
            reply = (
                "Live football updates are not configured yet. "
                "Please add the football API key on the server."
            )
        except FootballUnavailableError:
            reply = (
                "I cannot retrieve live football updates right now. "
                "Please try again soon."
            )

    if reply_path == "llm" and is_football_question(user_text):
        reply_path = "football_query_needs_detail"
        reply = (
            "I can check live football scores, FIFA World Cup winners, and top scorers. "
            "Try asking who won the World Cup, who scored the most goals, or which "
            "match is live now."
        )

    if reply_path == "llm":
        alarm_result = handle_alarm_voice(
            user_text,
            user_id=alarm_user_id,
            person_name=alarm_person_name,
            camera_identity_name=camera_identity_name,
            camera_identity_state=camera_identity_state,
            device_id=device_id,
        )
        if alarm_result.handled:
            reply_path = "alarm"
            logger.info("Voice alarm command | heard: %s", user_text[:120])
            reply = alarm_result.reply
            from alarm_service import get_alarm_service

            if get_alarm_service().get_reschedule_prompt_alarm() is not None:
                meta.prompt_medical_ack = True
    if reply_path == "llm" and is_servo_360_command(user_text):
        reply_path = "servo_360"
        logger.info("Voice servo 360 command | heard: %s", user_text[:120])
        if esp_servo_360_url(device_id or None) is None:
            reply = reply_for_servo_360_command(error="no_esp_url")
        else:
            meta.trigger_servo_360 = True
            reply = reply_for_servo_360_command()
    if reply_path == "llm" and is_last_question_query(user_text):
        from device_session import get_device_session_turns

        session_turns = get_device_session_turns(device_id)
        db_turns = list(memory_ctx.recent_history) if memory_ctx else []
        last_q = last_user_question_from_history(db_turns + session_turns)
        reply_path = "last_question"
        reply = answer_last_user_question(
            last_q,
            viewer_name=memory_name,
            has_face=bool(memory_name),
        )
        logger.info(
            "Voice last-question | viewer=%s last=%s heard: %s",
            memory_name or "(none)",
            (last_q or "")[:80],
            user_text[:120],
        )
    if reply_path == "llm" and is_conversation_recap_question(user_text):
        if not memory_name:
            reply_path = "recap_blocked_no_face"
            logger.info(
                "Voice recap blocked (no live recognized face) | state=%s heard: %s",
                camera_identity_state,
                user_text[:120],
            )
            reply = answer_conversation_recap(
                user_text,
                viewer_name=None,
                recognition_state=camera_identity_state,
                model=SETTINGS.ollama_model,
                api_url=SETTINGS.ollama_url,
                max_words=SETTINGS.recap_max_words,
                memory_context=None,
            )
        else:
            focus_topic = extract_recap_focus_topic(user_text)
            assumed_prior_topic = is_assumed_prior_topic_question(user_text)
            recap_context = _recap_context_from_recent_turns(
                memory_ctx.recent_history if memory_ctx else [],
                focus_topic=focus_topic if assumed_prior_topic else None,
            )
            if assumed_prior_topic:
                if not focus_topic or recap_context is None:
                    reply_path = "recap_not_found"
                    logger.info(
                        "Voice recap topic not in history | viewer=%s topic=%s heard: %s",
                        memory_name,
                        focus_topic or "(unknown)",
                        user_text[:120],
                    )
                    reply = recap_topic_not_found_reply(
                        focus_topic or "that topic",
                        person_name=memory_name,
                    )
                else:
                    follow_up = extract_recap_follow_up_question(user_text)
                    if follow_up:
                        reply_path = "recap_answer"
                        logger.info(
                            "Voice recap + question | viewer=%s topic=%s follow_up=%s heard: %s",
                            memory_name,
                            focus_topic,
                            follow_up[:80],
                            user_text[:120],
                        )
                        reply = answer_recap_contextual_question(
                            user_text,
                            follow_up,
                            viewer_name=memory_name,
                            memory_context=recap_context,
                            focus_topic=focus_topic,
                            model=SETTINGS.ollama_model,
                            api_url=SETTINGS.ollama_url,
                            max_words=max(SETTINGS.recap_max_words, SETTINGS.max_words_reply),
                        )
                    else:
                        reply_path = "recap"
                        logger.info(
                            "Voice recap topic confirmed | viewer=%s topic=%s heard: %s",
                            memory_name,
                            focus_topic,
                            user_text[:120],
                        )
                        reply = answer_conversation_recap(
                            user_text,
                            viewer_name=memory_name,
                            recognition_state=camera_identity_state,
                            model=SETTINGS.ollama_model,
                            api_url=SETTINGS.ollama_url,
                            max_words=SETTINGS.recap_max_words,
                            memory_context=recap_context,
                            focus_topic=focus_topic,
                        )
            else:
                follow_up = extract_recap_follow_up_question(user_text)
                if follow_up and recap_context:
                    reply_path = "recap_answer"
                    logger.info(
                        "Voice recap + question (general) | viewer=%s follow_up=%s heard: %s",
                        memory_name,
                        follow_up[:80],
                        user_text[:120],
                    )
                    reply = answer_recap_contextual_question(
                        user_text,
                        follow_up,
                        viewer_name=memory_name,
                        memory_context=recap_context,
                        focus_topic=focus_topic,
                        model=SETTINGS.ollama_model,
                        api_url=SETTINGS.ollama_url,
                        max_words=max(SETTINGS.recap_max_words, SETTINGS.max_words_reply),
                    )
                else:
                    reply_path = "recap"
                    logger.info(
                        "Voice recap query | viewer=%s turns=%s topic=%s | heard: %s",
                        memory_name,
                        memory_ctx.recent_turns if memory_ctx else 0,
                        focus_topic or "(general)",
                        user_text[:120],
                    )
                    reply = answer_conversation_recap(
                        user_text,
                        viewer_name=memory_name,
                        recognition_state=camera_identity_state,
                        model=SETTINGS.ollama_model,
                        api_url=SETTINGS.ollama_url,
                        max_words=SETTINGS.recap_max_words,
                        memory_context=recap_context,
                        focus_topic=focus_topic,
                    )
    elif reply_path == "llm" and memory_name and memory_ctx and memory_svc.ready:
        llm_memory = memory_svc.handle_llm_memory_turn(
            memory_ctx.user_id,
            user_text,
            person_name=memory_name,
            model=SETTINGS.ollama_model,
            api_url=SETTINGS.ollama_url,
        )
        if llm_memory:
            reply_path, reply = llm_memory
            logger.info(
                "Voice LLM memory | path=%s viewer=%s heard: %s",
                reply_path,
                memory_name,
                user_text[:120],
            )
    if reply_path == "llm" and is_identity_question(user_text):
        reply_path = "identity_llm"
        logger.info(
            "Voice identity query | state=%s name=%s | heard: %s",
            camera_identity_state,
            camera_identity_name or "(none)",
            user_text[:120],
        )
        reply = answer_identity_question(
            user_text,
            registered_name=camera_identity_name,
            recognition_state=camera_identity_state,
            model=SETTINGS.ollama_model,
            api_url=SETTINGS.ollama_url,
            max_words=SETTINGS.max_words_reply,
            memory_context=memory_ctx.prompt_block if memory_ctx else None,
        )
    elif reply_path == "llm":
        effective_viewer = _viewer_for_this_reply(viewer_name)
        if effective_viewer:
            logger.info(
                "Voice query (personalized) viewer: %s | heard: %s",
                effective_viewer,
                user_text[:120],
            )
        elif viewer_name:
            logger.info(
                "Voice query (generic; %s in frame) | heard: %s",
                viewer_name.strip(),
                user_text[:120],
            )
        else:
            logger.info("Voice query (no recognized viewer) | heard: %s", user_text[:120])
        from device_session import get_device_session_turns
        from math_voice import try_math_quiz_reply, try_spoken_math_reply

        session_turns = get_device_session_turns(device_id)
        math_reply = try_math_quiz_reply(
            user_text,
            device_id=device_id,
            session_turns=session_turns,
        ) or try_spoken_math_reply(user_text, session_turns=session_turns)
        if math_reply:
            reply_path = "math"
            reply = math_reply
            logger.info("Voice spoken math | heard: %s reply: %s", user_text[:80], reply[:80])
        else:
            topic_text = non_time_question_text(user_text)
            append_clock = topic_text is not None
            llm_text = topic_text or user_text
            if append_clock:
                logger.info(
                    "Voice compound topic + time | topic=%s heard: %s",
                    llm_text[:80],
                    user_text[:120],
                )
            memory_context, recent_history, follow_up = _voice_memory_context(
                device_id=device_id,
                memory_name=memory_name,
                memory_ctx=memory_ctx,
                user_text=llm_text,
                viewer_name=viewer_name,
                effective_viewer=effective_viewer,
            )
            reply = answer_voice_query(
                llm_text,
                viewer_name=effective_viewer,
                model=SETTINGS.ollama_model,
                api_url=SETTINGS.ollama_url,
                max_words=(
                    max(SETTINGS.max_words_reply, 60)
                    if follow_up or append_clock
                    else SETTINGS.max_words_reply
                ),
                memory_context=memory_context,
                recent_assistant_replies=_recent_assistant_replies(recent_history),
                is_follow_up=follow_up,
                vision_context=camera_scene,
            )
            if append_clock:
                reply = f"{reply.rstrip()} {local_server_time_reply()}"
    if reply_path in {"llm", "identity_llm", "memory_llm_store", "memory_llm_recall", "greeting", "smalltalk"}:
        reply = maybe_address_by_name(
            reply,
            memory_name or viewer_name,
            is_guest=ident_guest,
        )
    t_reply = time.perf_counter()

    memory_store = "skipped"
    if reply_path in {"llm", "identity_llm", "memory_llm_store", "recap_answer"}:
        memory_store = memory_svc.log_conversation_for_viewer(
            memory_name,
            user_text,
            reply,
            existing=memory_ctx,
            reply_path=reply_path,
            session_id=meta.session_id,
        )

    meta.eye_expression = infer_eye_expression_for_response(
        reply,
        user_text=user_text,
        reply_path=reply_path,
    )
    if reply_path == "session_greet" or (
        reply_path == "face_registration"
        and meta.registered_face_name
        and not ident_guest
    ):
        meta.eye_expression = "heart"
    elif reply_path in {
        "session_register_offer",
        "session_ask_name",
        "session_spell",
        "session_confirm",
        "face_registration",
    }:
        # Keep idle through hunt / "can I register you" / name prompts.
        if meta.eye_expression != "heart":
            meta.eye_expression = None
    from servo_tts_motion import motion_actions_for_reply

    meta.motion = motion_actions_for_reply(reply, reply_path=reply_path)
    t_pre_tts = time.perf_counter()

    wav, _voice = synthesize_sapi_wav_bytes(reply)
    tts_info = last_tts_synthesis_info()
    wav_out = resample_wav_bytes_to_mono_16bit(wav, VOICE_ASSIST_PLAYBACK_HZ)
    t_tts = time.perf_counter()

    stt_seconds = round(t_stt - t_start, 3)
    memory_seconds = round(t_memory - t_stt, 3)
    reply_seconds = round(t_reply - t_memory, 3)
    post_seconds = round(t_pre_tts - t_reply, 3)
    tts_seconds = round(t_tts - t_pre_tts, 3)
    process_total = round(t_tts - t_start, 3)
    audio_out_seconds = _wav_seconds(wav_out)
    meta.timings = {
        "heard": user_text[:200],
        "reply_text": reply[:200],
        "reply_path": reply_path,
        "audio_input_format": audio_input_format,
        "audio_in_seconds": audio_in_seconds,
        "audio_in_bytes": len(wav_bytes),
        "audio_out_seconds": audio_out_seconds,
        "audio_out_bytes": len(wav_out),
        "stt_engine": stt_engine,
        "tts_provider": tts_info["provider"],
        "tts_voice": tts_info["voice"],
        "tts_style": tts_info.get("style", ""),
        "stt_seconds": stt_seconds,
        "memory_seconds": memory_seconds,
        "reply_seconds": reply_seconds,
        "post_seconds": post_seconds,
        "tts_seconds": tts_seconds,
        "stages_sum_seconds": round(
            stt_seconds + memory_seconds + reply_seconds + post_seconds + tts_seconds, 3
        ),
        "process_total_seconds": process_total,
        "voice_viewer": viewer_name or "",
        "memory_viewer": memory_name or "",
        "memory_ready": memory_svc.ready,
        "memory_store": memory_store,
        "session": session,
        "session_id": meta.session_id,
        "wake_ok": True if session == "wake" else None,
        "heard_raw": heard_raw[:200],
        "turn": voice_turn,
        "energy": peak_energy,
    }
    if memory_ctx:
        meta.timings["memory_turns"] = memory_ctx.recent_turns
        meta.timings["memory_facts"] = memory_ctx.memory_count
        meta.timings["memory_user"] = memory_ctx.name
    if meta.eye_expression:
        meta.timings["eye_expression"] = meta.eye_expression
    if meta.motion:
        meta.timings["motion"] = meta.motion

    # Session stays open after every reply until the user says goodbye.
    # wake_reject never starts a session. Alarm medical-ack may already be set.
    _mark_continue_listen(meta, reply_path, user_text)
    meta.timings["continue_listen"] = bool(meta.prompt_medical_ack)

    from device_session import (
        append_device_session_turn,
        clear_device_session,
        get_device_session_turns,
    )
    from math_voice import clear_math_quiz
    from conversation_sessions import append_session_turn, end_session as persist_end_session

    if reply_path in DEVICE_SESSION_LOG_PATHS:
        append_device_session_turn(device_id, user_text, reply)
        if meta.session_id:
            append_session_turn(
                meta.session_id,
                device_id=device_id,
                user_name=memory_name or viewer_name,
                user_text=user_text,
                assistant_text=reply,
                reply_path=reply_path,
            )
    if is_conversation_goodbye(user_text) or reply_path == "goodbye":
        clear_device_session(device_id)
        clear_math_quiz(device_id)
        if meta.session_id:
            persist_end_session(
                meta.session_id,
                device_id=device_id,
                user_name=memory_name or viewer_name,
                reason="goodbye",
            )
    else:
        session_turns = get_device_session_turns(device_id)
        if session_turns:
            meta.timings["device_session_turns"] = len(session_turns)

    log_nino_voice(
        "SEND",
        turn=voice_turn,
        session=session,
        path=reply_path,
        continue_listen=int(bool(meta.prompt_medical_ack)),
        heard=user_text[:120],
        reply=(reply or "")[:120],
        engine=stt_engine,
        stt=meta.timings["stt_seconds"],
        llm=meta.timings["reply_seconds"],
        tts=meta.timings["tts_seconds"],
        total=meta.timings["process_total_seconds"],
        in_s=round(audio_in_seconds, 2),
        out_s=round(audio_out_seconds, 2),
        eye=meta.eye_expression or "",
    )
    pipeline_log(
        "TOTAL",
        "QUERY",
        path=reply_path,
        asr_s=meta.timings["stt_seconds"],
        llm_s=meta.timings["reply_seconds"],
        tts_s=meta.timings["tts_seconds"],
        heard=user_text[:120],
        reply=(reply or "")[:120],
        stage_s=meta.timings["process_total_seconds"],
    )
    return wav_out, meta
