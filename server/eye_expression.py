"""Map spoken assistant replies to NiNO eye expressions (sent to the device over WS)."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

EYE_EXPRESSIONS: tuple[str, ...] = (
    "happy",
    "tired",
    "curious",
    "sad",
    "surprised",
    "recalling",
)

BITMAP_EYE_EXPRESSIONS: tuple[str, ...] = (
    "tv",
    "radio",
    "bulb",
    "robot",
    "pencil",
    "sparkle",
    "fire",
    "bigsmile",
    "smile",
)

LLM_RESPONSE_PATHS: frozenset[str] = frozenset(
    {
        "llm",
        "identity_llm",
        "recap",
        "recap_answer",
        "recap_blocked_no_face",
        "joke",
        "joke_and_time",
        "football_joke",
        "look_scan_llm",
        "spatial_report",
        "observe_briefing",
        "memory_llm_recall",
        "memory_llm_store",
        "greeting",
        "smalltalk",
    }
)

LOOK_SCAN_PATHS: frozenset[str] = frozenset(
    {"look_scan", "look_scan_llm", "spatial_report", "observe_briefing"}
)

# Joke paths always show happy eyes, whatever words the punchline uses.
ALWAYS_HAPPY_PATHS: frozenset[str] = frozenset(
    {"joke", "joke_and_time", "football_joke"}
)
_VALID = frozenset(EYE_EXPRESSIONS) | frozenset({"heart"}) | frozenset(BITMAP_EYE_EXPRESSIONS)

# Keywords (substring) -> firmware bitmap eye tag.
_SPATIAL_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("television", " tv", "monitor", "screen"), "tv"),
    (("radio", "podcast"), "radio"),
    (("lamp", "light bulb", "bulb"), "bulb"),
    (("robot", " bot"), "robot"),
    (("pencil", " pen", "writing"), "pencil"),
    (("music", "song", "singing"), "radio"),
    (("fire", "flame", "burning"), "fire"),
    (("sparkle", "star", "magic", "twinkle"), "sparkle"),
    (("big smile", "grinning", "beaming"), "bigsmile"),
    (("smile", "smiling"), "smile"),
)


@dataclass(frozen=True)
class EyeContext:
    """Live context for picking an eye emoji beyond reply text alone."""

    person_name: str = ""
    camera_emotion: str = ""
    camera_scene: str = ""
    memory_turn_count: int = 0
    session_turn_count: int = 0
    is_follow_up: bool = False
    visible_names: tuple[str, ...] = field(default_factory=tuple)
    reply_path: str = "llm"

# User mood statements weigh more than incidental words in the assistant reply.
_USER_SCORE_MULTIPLIER = 2
_REPLY_SCORE_MULTIPLIER = 1

# When scores tie, prefer specific emotions over neutral curious.
_TIE_PRIORITY: tuple[str, ...] = (
    "surprised",
    "sad",
    "tired",
    "recalling",
    "happy",
    "curious",
)


@dataclass(frozen=True)
class _PhraseGroup:
    phrases: tuple[str, ...]
    weight: int


def _normalize_text(text: str) -> str:
    """Normalize STT/LLM text so phrase matching is consistent."""
    if not text:
        return ""
    cleaned = text.strip()
    cleaned = cleaned.replace("\u2019", "'").replace("\u2018", "'")
    cleaned = cleaned.replace("\u201c", '"').replace("\u201d", '"')
    cleaned = cleaned.replace("\u2014", "-").replace("\u2013", "-")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned


def _compile_phrase_pattern(phrases: tuple[str, ...]) -> re.Pattern[str]:
    """Build one regex from many phrases (longest first to prefer multi-word matches)."""
    ordered = sorted({p.strip().lower() for p in phrases if p.strip()}, key=len, reverse=True)
    if not ordered:
        return re.compile(r"(?!x)x")
    parts: list[str] = []
    for phrase in ordered:
        tokens = phrase.split()
        if len(tokens) == 1:
            parts.append(rf"\b{re.escape(tokens[0])}\b")
        else:
            inner = r"\s+".join(re.escape(t) for t in tokens)
            parts.append(rf"\b{inner}\b")
    return re.compile("|".join(f"(?:{p})" for p in parts), re.I)


# --- Phrase lists (add new phrases here) -----------------------------------

_HAPPY = (
    _PhraseGroup(
        (
            "congratulations",
            "well done",
            "good job",
            "nice work",
            "great news",
            "good news",
            "sounds good",
            "sounds great",
            "love that",
            "you're welcome",
            "you are welcome",
            "have a great",
            "have a good",
            "have a nice",
            "hope you enjoy",
            "glad to hear",
            "happy to help",
            "that is funny",
            "that's funny",
            "made me laugh",
            "nice to meet",
            "glad you're here",
            "glad you are here",
            "that made me smile",
        ),
        4,
    ),
    _PhraseGroup(
        (
            "great",
            "awesome",
            "wonderful",
            "lovely",
            "fantastic",
            "excellent",
            "perfect",
            "brilliant",
            "delighted",
            "pleased",
            "celebrate",
            "congrats",
            "hooray",
            "yay",
            "cheers",
            "fun",
            "enjoy",
            "smile",
            "laugh",
            "joke",
            "funny",
            "hilarious",
            "nice",
            "glad",
            "happy",
            "super",
            "amazing",
            "love it",
            "lovely day",
            "brighten",
            "brighter",
            "feel better",
            "cheer you up",
            "pick you up",
        ),
        2,
    ),
)

_TIRED = (
    _PhraseGroup(
        (
            "feeling tired",
            "feel tired",
            "i am tired",
            "i'm tired",
            "so tired",
            "very tired",
            "feeling sleepy",
            "feel sleepy",
            "need sleep",
            "need rest",
            "get some rest",
            "time to sleep",
            "good night",
            "late night",
            "long day",
            "worn out",
            "run down",
            "burned out",
            "burnt out",
            "need a break",
            "off to bed",
        ),
        4,
    ),
    _PhraseGroup(
        (
            "tired",
            "sleepy",
            "exhausted",
            "fatigue",
            "weary",
            "drained",
            "bed",
            "nap",
            "sleep",
            "yawn",
            "rest",
            "relax",
            "slow down",
            "take it easy",
            "tuck in",
            "burnout",
            "stay up",
            "slept",
            "insomnia",
            "drowsy",
        ),
        2,
    ),
)

_SAD = (
    _PhraseGroup(
        (
            "i feel sad",
            "feeling sad",
            "feel sad",
            "i am sad",
            "i'm sad",
            "so sad",
            "very sad",
            "feel down",
            "feeling down",
            "feel low",
            "feeling low",
            "feel awful",
            "feeling awful",
            "feel terrible",
            "feeling terrible",
            "feel horrible",
            "hard time",
            "rough day",
            "bad day",
            "sorry to hear",
            "sorry you're",
            "sorry you are",
            "sorry about that",
            "i'm sorry",
            "i am sorry",
            "can't help",
            "cannot help",
            "not able to",
            "doesn't work",
            "does not work",
            "won't work",
            "will not work",
            "not available",
            "no luck",
            "out of luck",
            "not feeling well",
            "under the weather",
            "feeling sick",
        ),
        4,
    ),
    _PhraseGroup(
        (
            "sorry",
            "unfortunately",
            "sadly",
            "sad",
            "upset",
            "unhappy",
            "miserable",
            "depressed",
            "lonely",
            "alone",
            "miss you",
            "missing",
            "loss",
            "grief",
            "regret",
            "disappointed",
            "disappointing",
            "can't",
            "cannot",
            "unable",
            "failed",
            "failure",
            "problem",
            "error",
            "issue",
            "trouble",
            "hurt",
            "pain",
            "ache",
            "worried",
            "concerned",
            "anxious",
            "afraid",
            "scared",
            "fear",
            "struggle",
            "difficult",
            "tough",
            "broken",
            "damaged",
            "unavailable",
            "declined",
            "denied",
            "apologize",
            "apology",
            "heartbreaking",
            "bless you",
        ),
        2,
    ),
)

_SURPRISED = (
    _PhraseGroup(
        (
            "oh my",
            "oh wow",
            "no way",
            "never knew",
            "didn't know",
            "did not know",
            "had no idea",
            "that's new",
            "that is new",
            "plot twist",
            "believe that",
            "can you believe",
            "are you serious",
            "you kidding",
        ),
        4,
    ),
    _PhraseGroup(
        (
            "wow",
            "whoa",
            "gosh",
            "goodness",
            "surprise",
            "surprised",
            "surprising",
            "unexpected",
            "incredible",
            "unbelievable",
            "astonishing",
            "shocking",
            "suddenly",
        ),
        2,
    ),
)

_CURIOUS = (
    _PhraseGroup(
        (
            "good question",
            "tell me more",
            "let me think",
            "let me explain",
            "not sure",
            "don't know",
            "do not know",
            "hard to say",
            "it depends",
            "depends on",
            "what if",
            "how about",
            "would you like",
            "do you want",
            "want to know",
            "did you mean",
            "in other words",
            "for example",
            "that means",
            "here is",
            "here's",
            "here are",
        ),
        3,
    ),
    _PhraseGroup(
        (
            "curious",
            "interesting",
            "intriguing",
            "fascinating",
            "wonder",
            "wondering",
            "maybe",
            "perhaps",
            "possibly",
            "explore",
            "find out",
            "look into",
            "investigate",
            "generally",
            "usually",
            "often",
            "sometimes",
            "typically",
            "explain",
            "describe",
            "define",
            "meaning",
            "hmm",
        ),
        2,
    ),
)

_RECALLING = (
    _PhraseGroup(
        (
            "you asked",
            "you said",
            "you mentioned",
            "you told",
            "we talked",
            "we discussed",
            "as we discussed",
            "as i said",
            "as i mentioned",
            "from our chat",
            "from our conversation",
            "from our talk",
            "last time",
            "last conversation",
            "last chat",
            "last week",
            "last month",
            "last year",
            "the other day",
            "looking back",
            "you wanted to know",
            "you were asking",
            "we covered",
            "earlier today",
            "earlier you",
            "our last chat",
            "from before",
        ),
        4,
    ),
    _PhraseGroup(
        (
            "remember",
            "recall",
            "recap",
            "summarize",
            "summary",
            "previously",
            "before",
            "earlier",
            "yesterday",
            "ago",
            "history",
            "back when",
            "conversation about",
            "talked about",
            "discussed",
            "mentioned",
        ),
        2,
    ),
)

# Extra shape-based rules (not plain vocabulary).
_EXTRA_REGEX_RULES: dict[str, list[tuple[re.Pattern[str], int]]] = {
    "curious": [(re.compile(r"\?"), 2)],
    "surprised": [
        (re.compile(r"!{2,}"), 3),
        (re.compile(r"!\s*$"), 2),
    ],
}


def _compile_expression_rules(
    groups: tuple[_PhraseGroup, ...],
) -> list[tuple[re.Pattern[str], int]]:
    return [(_compile_phrase_pattern(group.phrases), group.weight) for group in groups]


_SCORE_RULES: dict[str, list[tuple[re.Pattern[str], int]]] = {
    "happy": _compile_expression_rules(_HAPPY),
    "tired": _compile_expression_rules(_TIRED),
    "sad": _compile_expression_rules(_SAD),
    "surprised": _compile_expression_rules(_SURPRISED),
    "curious": _compile_expression_rules(_CURIOUS),
    "recalling": _compile_expression_rules(_RECALLING),
}
for _expr, _rules in _EXTRA_REGEX_RULES.items():
    _SCORE_RULES[_expr].extend(_rules)


def normalize_eye_expression(value: str | None) -> str | None:
    cleaned = (value or "").strip().lower()
    if cleaned in _VALID:
        return cleaned
    return None


def spatial_eye_from_text(*texts: str) -> str | None:
    """Map mentioned objects/topics to firmware bitmap eyes (tv, bulb, …)."""
    combined = " ".join(str(t or "").strip() for t in texts if t).lower()
    if not combined:
        return None
    best_score = 0
    best_eye: str | None = None
    for keywords, eye in _SPATIAL_RULES:
        for kw in keywords:
            if kw in combined and len(kw) > best_score:
                best_score = len(kw)
                best_eye = eye
    if best_eye and normalize_eye_expression(best_eye):
        return best_eye
    return None


def emotion_eye_from_context(camera_emotion: str) -> str | None:
    """DrGM/Rekognition mood string -> animated eye tag."""
    low = str(camera_emotion or "").strip().lower()
    if not low:
        return None
    for phrase, eye in (
        ("looks happy", "happy"),
        ("looks surprise", "surprised"),
        ("looks sad", "sad"),
        ("looks angry", "sad"),
        ("looks fear", "surprised"),
        ("looks disgust", "sad"),
        (" looks happy", "happy"),
        (" looks sad", "sad"),
        (" looks surprise", "surprised"),
    ):
        if phrase in low and eye:
            return eye
    return None


def spatial_eye_from_context(
    ctx: EyeContext,
    *,
    user_text: str = "",
    reply_text: str = "",
) -> str | None:
    """Spatial/object emoji when the user, reply, or look-scan scene mentions it."""
    direct = spatial_eye_from_text(user_text, reply_text)
    if direct:
        return direct
    if ctx.reply_path in LOOK_SCAN_PATHS:
        return spatial_eye_from_text(ctx.camera_scene)
    return None


def _apply_context_biases(scores: dict[str, int], ctx: EyeContext) -> None:
    if ctx.reply_path in {"memory_llm_recall", "recap", "recap_answer", "recap_blocked_no_face"}:
        scores["recalling"] += 5
    if ctx.is_follow_up or ctx.memory_turn_count >= 3 or ctx.session_turn_count >= 2:
        scores["recalling"] += 2
    if ctx.person_name and ctx.reply_path in {"greeting", "smalltalk", "session_greet"}:
        scores["happy"] += 2
    mood_eye = emotion_eye_from_context(ctx.camera_emotion)
    if mood_eye:
        scores[mood_eye] += 3


IDENTIFY_HEART_PATHS: frozenset[str] = frozenset({"session_greet"})
# Hunt / "can I register you" / name-spell-confirm: no LCD emoji until identified.
IDENTIFY_IDLE_PATHS: frozenset[str] = frozenset(
    {
        "session_register_offer",
        "session_ask_name",
        "session_spell",
        "session_confirm",
        "session_letter",
        "face_registration",
    }
)


def infer_eye_expression_for_response(
    reply_text: str,
    *,
    user_text: str = "",
    reply_path: str = "llm",
    context: EyeContext | None = None,
    person_name: str = "",
    camera_emotion: str = "",
    camera_scene: str = "",
    memory_turn_count: int = 0,
    session_turn_count: int = 0,
    is_follow_up: bool = False,
    visible_names: tuple[str, ...] | list[str] | None = None,
) -> str | None:
    """Return an expression for LLM replies; None leaves the bot on idle."""
    ctx = context or EyeContext(
        person_name=person_name,
        camera_emotion=camera_emotion,
        camera_scene=camera_scene,
        memory_turn_count=memory_turn_count,
        session_turn_count=session_turn_count,
        is_follow_up=is_follow_up,
        visible_names=tuple(visible_names or ()),
        reply_path=reply_path,
    )
    if reply_path in IDENTIFY_HEART_PATHS:
        return "heart"
    if reply_path in IDENTIFY_IDLE_PATHS:
        return None
    if reply_path not in LLM_RESPONSE_PATHS and reply_path not in LOOK_SCAN_PATHS:
        return None
    try:
        spatial = spatial_eye_from_context(
            ctx, user_text=user_text, reply_text=reply_text
        )
        tag = infer_eye_expression(
            reply_text,
            user_text=user_text,
            reply_path=reply_path,
            context=ctx,
        )
        normalized = normalize_eye_expression(tag)
        if spatial and (
            reply_path in LOOK_SCAN_PATHS
            or not normalized
            or normalized == "curious"
        ):
            return spatial
        return normalized
    except Exception:
        logger.exception("Eye expression inference failed; omitting tag")
        return None


def _score_text(text: str, multiplier: int) -> dict[str, int]:
    scores = {name: 0 for name in EYE_EXPRESSIONS}
    if not text or multiplier <= 0:
        return scores
    for name, rules in _SCORE_RULES.items():
        for pattern, weight in rules:
            hits = len(pattern.findall(text))
            if hits:
                scores[name] += hits * weight * multiplier
    return scores


def _merge_scores(base: dict[str, int], extra: dict[str, int]) -> dict[str, int]:
    return {name: base[name] + extra[name] for name in EYE_EXPRESSIONS}


def _pick_best(scores: dict[str, int]) -> str | None:
    best_score = max(scores.values())
    if best_score <= 0:
        return None

    ranked = sorted(
        ((name, score) for name, score in scores.items() if score > 0),
        key=lambda item: (-item[1], _TIE_PRIORITY.index(item[0])),
    )
    if not ranked:
        return None

    top_name, top_score = ranked[0]
    if len(ranked) == 1:
        return top_name

    second_score = ranked[1][1]
    # Strong lead — take the winner.
    if top_score >= second_score + 2:
        return top_name

    # Close call: prefer emotional tags over curious unless curious is clearly ahead.
    if top_name == "curious" and ranked[1][0] != "curious":
        return ranked[1][0]
    if ranked[1][0] == "curious" and top_name != "curious" and top_score >= second_score:
        return top_name

    return top_name


def _fallback_from_reply(reply: str, user: str, reply_path: str) -> str:
    if reply_path in {"recap", "recap_blocked_no_face"}:
        return "recalling"

    words = reply.split()
    word_count = len(words)
    user_l = user.lower()
    reply_l = reply.lower()

    if any(
        p in user_l
        for p in (
            "feel sad",
            "feeling sad",
            "feel down",
            "feeling down",
            "depressed",
            "lonely",
            "upset",
            "bad day",
            "rough day",
        )
    ):
        return "sad"
    if any(
        p in user_l
        for p in (
            "feel tired",
            "feeling tired",
            "so sleepy",
            "need sleep",
            "good night",
        )
    ):
        return "tired"
    if any(p in user_l for p in ("joke", "funny", "laugh", "cheer me up", "make me smile")):
        return "happy"
    if any(
        p in user_l
        for p in (
            "remember",
            "recap",
            "what did we talk",
            "what we talked",
            "last time",
            "earlier",
        )
    ):
        return "recalling"

    if user.endswith("?") or reply.endswith("?"):
        return "curious"
    if reply.rstrip().endswith("!") and word_count <= 14:
        return "surprised"
    if word_count >= 40:
        return "recalling"
    if word_count >= 22 and re.search(r"\b(you|your|we|our|when|then|after)\b", reply, re.I):
        return "recalling"
    if any(
        p in reply_l
        for p in (
            "i don't know",
            "not certain",
            "it varies",
            "depends on",
            "let me explain",
            "here is",
            "here's",
            "that is",
            "that's",
        )
    ):
        return "curious"
    if re.search(r"\b(yes|no|okay|ok|sure|right|correct|exactly)\b", reply_l) and word_count <= 8:
        return "curious"

    return "curious"


def infer_eye_expression(
    reply_text: str,
    *,
    user_text: str = "",
    reply_path: str = "llm",
    context: EyeContext | None = None,
) -> str:
    reply = _normalize_text(reply_text)
    user = _normalize_text(user_text)
    ctx = context or EyeContext(reply_path=reply_path)

    if reply_path in ALWAYS_HAPPY_PATHS:
        return "happy"

    if not reply:
        base = _fallback_from_reply("", user, reply_path)
        mood = emotion_eye_from_context(ctx.camera_emotion)
        return mood or base

    scores = _score_text(reply, _REPLY_SCORE_MULTIPLIER)
    scores = _merge_scores(scores, _score_text(user, _USER_SCORE_MULTIPLIER))
    _apply_context_biases(scores, ctx)

    if reply_path in {"recap", "recap_blocked_no_face", "recap_answer"}:
        scores["recalling"] += 6

    best = _pick_best(scores)
    if best is not None:
        return best

    mood = emotion_eye_from_context(ctx.camera_emotion)
    if mood:
        return mood

    return _fallback_from_reply(reply, user, reply_path)
