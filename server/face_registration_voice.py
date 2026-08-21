"""Extract a person's name from spoken registration phrases."""

from __future__ import annotations

import re
import unicodedata

# Letter from any script, then letters / non-ASCII marks / spaces / hyphens / apostrophes.
# Non-ASCII continuation is required so Devanagari vowel signs (Mc/Mn) are kept.
_NAME_CAPTURE = r"([^\W\d_](?:[^\W\d_]|[^\x00-\x7F]|[\s'\-]){0,62})"

# Ordered — first match wins. Bare name is last and validated more strictly.
_FRAMED_NAME_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE | re.UNICODE)
    for p in (
        rf"\b(?:my|the)\s+name\s+is\s+{_NAME_CAPTURE}",
        rf"\b(?:i\s*am|i'?m|im)\s+{_NAME_CAPTURE}",
        rf"\b(?:call|calling)\s+me\s+{_NAME_CAPTURE}",
        rf"\b(?:this|that)\s+is\s+{_NAME_CAPTURE}",
        rf"\b(?:it'?s|its)\s+{_NAME_CAPTURE}",
        rf"\bname\s+is\s+{_NAME_CAPTURE}",
    )
)
_BARE_NAME_PATTERN = re.compile(rf"^{_NAME_CAPTURE}$", re.IGNORECASE | re.UNICODE)

# Spoken intros with no name yet (device VAD often ends mid-phrase).
_INCOMPLETE_NAME_PHRASE = re.compile(
    r"^\s*(?:(?:hi|hello|hey)[,!]?\s+)*"
    r"(?:"
    r"(?:my|the)\s+name\s+is"
    r"|i\s*am|i'?m|im"
    r"|(?:please\s+)?(?:call|calling)\s+me"
    r"|(?:this|that)\s+is"
    r"|it'?s|its"
    r"|name\s+is"
    r")"
    r"[\s.,!?…]*$",
    re.IGNORECASE,
)

# NiNO's own registration prompts, often picked up by the mic after TTS.
_FACE_REG_ECHO_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bcatch your name\b",
        r"\bsay (?:it|your name) again\b",
        r"\bhaven'?t registered\b",
        r"\bplease say (?:your name|it)\b",
        r"\bsay your name to continue\b",
        r"\bafter the beep\b",
        r"\bi didn'?t (?:quite )?catch\b",
        r"\bi haven'?t heard\b",
        r"\bregistered your face yet\b",
        r"\bplease say just your name\b",
        r"\bsay:?\s*my name is\b",
        r"\bmy name is,?\s+then\b",
        r"\bmy name is,?\s+and then\b",
        r"\bplease say your name\b",
        r"\bsay your name\b",
        # Entertaining unknown-face prompt variants
        r"\bmystery guest\b",
        r"\bplot twist\b",
        r"\bfresh face alert\b",
        r"\bhold up\b.*\bwho are you\b",
        r"\bsense a new friend\b",
        r"\bstranger danger\b",
        r"\bwelcome to the show\b",
        r"\bdrop your name\b",
        r"\bintroduce yourself\b",
        r"\btell me what to call you\b",
        r"\blooks like you are a new user\b",
        r"\bcan i register you\b",
        r"\bwhat should i call you\b",
        r"\bspell that name\b",
        r"\bplease spell\b",
        r"\bis that right\b",
        r"\bkeep this as a guest\b",
        r"\bi'?ve registered you\b",
    )
)

# GREET TTS fragments (no "?") — opening echo only, not registration prompts.
_GREET_PHRASE_ECHO_RE = re.compile(
    r"^\s*(?:"
    r"how can i help you|"
    r"how are you today|"
    r"hey\b.+\bhow can i help you"
    r")\s*[.!?…]*\s*$",
    re.IGNORECASE,
)

# Full GREET lines always dropped — do not consume the opening-echo budget.
_UNCONDITIONAL_GREET_ECHO_RE = re.compile(
    r"^\s*hey\b.+\bhow can i help you\s*[.!?…]*\s*$",
    re.IGNORECASE,
)


def is_unconditional_greet_echo(user_text: str) -> bool:
    text = (user_text or "").strip()
    if not text or "?" in text:
        return False
    return bool(_UNCONDITIONAL_GREET_ECHO_RE.match(text))

# GREET leftover is "Hey <name>" (or the full "how can I help you" prompt).
# Bare hello / hi / good morning is the user's greeting — do not skip it.
_OPENING_NAMED_HELLO_RE = re.compile(
    r"^\s*(?:hey|hi|hello)\s*,?\s*(?P<name>[A-Za-z]{2,20})[.!?…]*\s*$",
    re.IGNORECASE,
)
_OPENING_HELLO_FILLER = frozenset(
    {"there", "everyone", "all", "folks", "guys", "team"}
)

# Always reject — never a usable display name (framed or bare).
_REJECT_NAMES: frozenset[str] = frozenset(
    {
        "unknown",
        "face",
        "hello",
        "hi",
        "hey",
        "yes",
        "no",
        "yeah",
        "yep",
        "nope",
        "ok",
        "okay",
        "register",
        "registration",
        "name",
        "nino",
        "esp",
        "robot",
        "bot",
        "please",
        "thanks",
        "thank",
        "you",
        "me",
        "my",
        "the",
        "a",
        "an",
        "again",
        "catch",
        "quite",
        "continue",
        "beep",
        "soon",
        "see",
        "enough",
        "turn",
        "off",
        "turn off",
        "see you",
        "see you soon",
        "see you soon again",
        "agree",
        "greek",
        "cha",  # common truncated STT (e.g. Chakri)
        "joke",
        "jokes",
        "peace",
        "date",
        "stop",
        "start",
        "cancel",
        "wait",
        "what",
        "who",
        "why",
        "how",
        "when",
        "where",
        "help",
        "sorry",
        "excuse",
        "repeat",
        "nothing",
        "something",
        "anything",
        "everything",
        "none",
        "null",
        "test",
        "testing",
        "asdf",
        "abc",
        "xyz",
        # Fragments from entertaining registration prompts
        "mystery",
        "guest",
        "friend",
        "stranger",
        "danger",
        "kidding",
        "twist",
        "alert",
        "show",
        "ooh",
        "fresh",
    }
)

# Extra bare-word rejects: common STT garbage / English that is rarely a person name.
# Framed phrases ("my name is Hope") may still accept overlap names deliberately omitted here.
_BARE_NAME_REJECT: frozenset[str] = frozenset(
    {
        "agree",
        "greek",
        "enough",
        "right",
        "left",
        "good",
        "bad",
        "fine",
        "great",
        "cool",
        "nice",
        "sure",
        "maybe",
        "really",
        "actually",
        "basically",
        "literally",
        "whatever",
        "anyway",
        "today",
        "tomorrow",
        "yesterday",
        "morning",
        "evening",
        "night",
        "weather",
        "music",
        "camera",
        "microphone",
        "speaker",
        "volume",
        "louder",
        "softer",
        "quiet",
        "silent",
        "listen",
        "speaking",
        "talking",
        "looking",
        "ready",
        "done",
        "finish",
        "finished",
        "next",
        "back",
        "home",
        "open",
        "close",
        "play",
        "pause",
        "skip",
        "continue",
        "retry",
        "try",
        "tell",
        "say",
        "said",
        "ask",
        "asked",
        "call",
        "calling",
        "come",
        "go",
        "going",
        "gone",
        "here",
        "there",
        "over",
        "under",
        "after",
        "before",
        "because",
        "about",
        "around",
        "people",
        "person",
        "human",
        "someone",
        "everyone",
        "anyone",
        "nobody",
        "english",
        "indian",
        "hindi",
        "telugu",
        "tamil",
        "one",
        "two",
        "three",
        "four",
        "five",
        "six",
        "seven",
        "eight",
        "nine",
        "ten",
        "first",
        "second",
        "third",
        "last",
        "new",
        "old",
        "big",
        "small",
        "true",
        "false",
        "correct",
        "wrong",
        "yes",
        "no",
        "cha",  # common truncated STT for Chakri / similar
    }
)

_TRAILING_NOISE = re.compile(
    r"\s+(?:please|thanks|thank you|here|sir|ma'am|maam)\s*$",
    re.IGNORECASE,
)

# Command-like utterances that should never become a name.
_COMMANDISH = re.compile(
    r"\b(?:"
    r"turn\s+off|turn\s+on|shut\s+up|tell\s+me|give\s+me|"
    r"play\s+|stop\s+|cancel|volume|louder|softer|"
    r"what(?:'s|\s+is)\s+|who\s+am\s+i|how\s+are\s+you|"
    r"see\s+you|good\s+(?:morning|night|evening|bye)|"
    r"thank\s+you|excuse\s+me"
    r")\b",
    re.IGNORECASE,
)

# User declines face registration after the name prompt (whole utterance).
_CANCEL_UTTERANCE = re.compile(
    r"^\s*(?:"
    r"no|nope|nah|"
    r"cancel(?:\s+it)?|stop(?:\s+it)?|quit|abort|"
    r"shut\s*up|be\s+quiet|"
    r"never\s*mind|forget\s+it|"
    r"no\s+thanks|no\s+thank\s+you|"
    r"not\s+now|go\s+away|leave\s+me\s+alone|"
    r"enough|"
    r"don'?t(?:\s+want(?:\s+to)?)?|"
    r"do\s+not(?:\s+want(?:\s+to)?)?|"
    r"i\s+(?:don'?t|do\s+not)\s+want(?:\s+to(?:\s+register)?)?|"
    r"please\s+(?:stop|cancel|don'?t)"
    r")"
    r"(?:\s+(?:please|it|this|registration|now))*"
    r"\s*[.!?…]*\s*$",
    re.IGNORECASE,
)

# Clear cancel intent inside a short refusal sentence.
_CANCEL_IN_SENTENCE = re.compile(
    r"\b(?:"
    r"shut\s*up|"
    r"cancel(?:\s+(?:it|this|registration))?|"
    r"never\s*mind|forget\s+it|"
    r"don'?t\s+(?:want\s+to\s+)?register|"
    r"stop\s+(?:it|this|registration|the\s+process)|"
    r"cancel\s+(?:the\s+)?(?:process|registration)|"
    r"leave\s+me\s+alone"
    r")\b",
    re.IGNORECASE,
)

# User says they are already known — re-run recognition instead of guest/register.
_ALREADY_REGISTERED_CLAIM_RE = re.compile(
    r"\b(?:"
    r"i(?:'?m| am)\s+not\s+(?:a\s+)?new(?:\s+user)?"
    r"|not\s+a\s+new\s+user"
    r"|i(?:'?m| am)\s+already\s+registered"
    r"|i\s+already\s+registered"
    r"|you(?:'?ve| have)?\s+(?:already\s+)?(?:know|seen|recogni[sz]e)(?:d)?\s+me"
    r"|you\s+know\s+(?:me|who\s+i\s+am)"
    r"|i(?:'?m| am)\s+(?:already\s+)?(?:a\s+)?(?:registered|known)\s+user"
    r"|you\s+already\s+know\s+me"
    r")\b",
    re.IGNORECASE,
)

_MIN_ASCII_LETTERS = 3
_MIN_ASCII_LETTERS_BARE = 4


def _strip_trailing_punct(text: str) -> str:
    return text.strip().strip(".,!?…\"'“”‘’")


def _clean_candidate(raw: str) -> str:
    text = _strip_trailing_punct(raw)
    text = _TRAILING_NOISE.sub("", text).strip()
    # Title-case Latin words; leave other scripts as spoken/STT provided.
    parts = [p for p in re.split(r"\s+", text) if p]
    if not parts:
        return ""
    if len(parts) > 3:
        parts = parts[:3]
    cleaned: list[str] = []
    for part in parts:
        if part.isascii() and part.isalpha():
            cleaned.append(part[:1].upper() + part[1:].lower())
        else:
            cleaned.append(part)
    return " ".join(cleaned)


def _has_letter(text: str) -> bool:
    return any(unicodedata.category(ch).startswith("L") for ch in text)


def _letter_count(text: str) -> int:
    return sum(1 for ch in text if unicodedata.category(ch).startswith("L"))


def _is_ascii_alpha_name(text: str) -> bool:
    return bool(text) and all(
        (ch.isalpha() and ch.isascii()) or ch in " -'" for ch in text
    )


def _is_valid_name(candidate: str, *, bare: bool) -> bool:
    lower = candidate.lower().strip()
    if not lower:
        return False
    if lower in _REJECT_NAMES:
        return False
    words = lower.split()
    if not words or len(words) > 3:
        return False
    if any(w in _REJECT_NAMES for w in words):
        return False
    if "register" in lower or "face" in lower:
        return False
    if not _has_letter(candidate):
        return False
    if any(ch.isdigit() for ch in candidate):
        return False
    if _COMMANDISH.search(candidate):
        return False

    letters = _letter_count(candidate)
    if _is_ascii_alpha_name(candidate):
        min_letters = _MIN_ASCII_LETTERS_BARE if bare else _MIN_ASCII_LETTERS
        if letters < min_letters:
            return False
        # Reject all-caps acronyms / initials like "OK", "TV".
        compact = candidate.replace(" ", "").replace("-", "").replace("'", "")
        if compact.isupper() and len(compact) <= 3:
            return False
    elif letters < 2:
        return False

    if bare:
        if lower in _BARE_NAME_REJECT:
            return False
        if any(w in _BARE_NAME_REJECT for w in words):
            return False
        # Bare path: single token only (avoids "turn off", "see you").
        if len(words) != 1:
            return False

    return True


def is_incomplete_name_phrase(user_text: str) -> bool:
    """True when STT captured a name intro without the actual name."""
    text = _strip_trailing_punct(user_text or "")
    if not text:
        return False
    return bool(_INCOMPLETE_NAME_PHRASE.match(text))


def is_face_reg_prompt_echo(user_text: str) -> bool:
    """True when STT looks like NiNO's own registration prompt."""
    text = (user_text or "").strip()
    if not text:
        return False
    return any(p.search(text) for p in _FACE_REG_ECHO_PATTERNS)


def is_opening_greeting_echo(user_text: str) -> bool:
    """True when STT looks like session GREET TTS, not a new user hello."""
    text = (user_text or "").strip()
    if not text:
        return False
    if is_face_reg_prompt_echo(text):
        return True
    if "?" in text or len(text) > 48:
        return False
    if _GREET_PHRASE_ECHO_RE.match(text):
        return True
    match = _OPENING_NAMED_HELLO_RE.match(text)
    if match is None:
        return False
    name = (match.group("name") or "").strip().lower()
    return bool(name) and name not in _OPENING_HELLO_FILLER


_SESSION_END_RE = re.compile(
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
_SESSION_STOP_RE = re.compile(
    r"^\s*(?:please\s+)?stop(?:\s+(?:now|please))?[.!?]*\s*$",
    re.IGNORECASE,
)


def is_session_goodbye_utterance(user_text: str) -> bool:
    """True farewell — end the whole session (not a registration cancel)."""
    text = (user_text or "").strip()
    if not text:
        return False
    return bool(_SESSION_END_RE.search(text))


def is_session_end_utterance(user_text: str) -> bool:
    """Goodbye / stop — end the session outside of registration."""
    text = (user_text or "").strip()
    if not text:
        return False
    if _SESSION_STOP_RE.match(text):
        return True
    return is_session_goodbye_utterance(text)


def is_registration_stop_process(user_text: str) -> bool:
    """Cancel or stop registration without treating a bare 'no' as abort.

    Bare 'no' still means decline on the offer, or 'wrong name' on confirm.
    """
    if is_session_goodbye_utterance(user_text):
        return False
    text = _strip_trailing_punct(user_text or "")
    if not text:
        return False
    if re.match(r"^\s*(?:no|nope|nah)\b", text, re.IGNORECASE) and not re.search(
        r"\b(?:cancel|stop|quit|abort|never\s*mind|forget)\b", text, re.IGNORECASE
    ):
        return False
    return is_registration_cancel(text)


def is_already_registered_claim(user_text: str) -> bool:
    """True when the user says they are not new / already known."""
    text = _strip_trailing_punct(user_text or "")
    if not text or len(text) > 100:
        return False
    return bool(_ALREADY_REGISTERED_CLAIM_RE.search(text))


def is_registration_cancel(user_text: str) -> bool:
    """True when the user declines / cancels face registration."""
    text = _strip_trailing_punct(user_text or "")
    if not text or len(text) > 80:
        return False
    # Framed name answers win over cancel words inside them.
    if any(p.search(text) for p in _FRAMED_NAME_PATTERNS):
        return False
    if _CANCEL_UTTERANCE.match(text):
        return True
    return bool(_CANCEL_IN_SENTENCE.search(text))


def extract_registration_name(user_text: str) -> str | None:
    """Return a display name from STT text, or None if no confident name."""
    text = (user_text or "").strip()
    if not text or len(text) > 120:
        return None
    if is_face_reg_prompt_echo(text) or is_incomplete_name_phrase(text):
        return None
    if _COMMANDISH.search(text) and not any(
        p.search(text) for p in _FRAMED_NAME_PATTERNS
    ):
        # "turn off", "tell me a joke", etc. — not a name utterance.
        return None

    for pattern in _FRAMED_NAME_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        candidate = _clean_candidate(match.group(1))
        if candidate and _is_valid_name(candidate, bare=False):
            return candidate

    match = _BARE_NAME_PATTERN.match(_strip_trailing_punct(text))
    if match:
        candidate = _clean_candidate(match.group(1))
        if candidate and _is_valid_name(candidate, bare=True):
            return candidate

    return None


_OFFER_YES_RE = re.compile(
    r"^\s*(?:yes|yeah|yep|yup|sure|ok|okay|please|please do|go ahead|"
    r"register|i would|i'?d like to|let'?s do it)\b",
    re.IGNORECASE,
)
_OFFER_NO_RE = re.compile(
    r"^\s*(?:no|nope|nah|skip|later|not now|guest|maybe later)\b",
    re.IGNORECASE,
)
_CONFIRM_YES_RE = re.compile(
    r"^\s*(?:yes|yeah|yep|yup|that'?s right|that is right|correct|good|"
    r"ok|okay|perfect|exactly|sounds good)\b",
    re.IGNORECASE,
)
_CONFIRM_NO_RE = re.compile(
    r"^\s*(?:no|nope|nah|wrong|try again|not quite|that'?s wrong|incorrect)\b",
    re.IGNORECASE,
)

_LETTER_WORDS: dict[str, str] = {
    "a": "A",
    "ay": "A",
    "eh": "A",
    "alpha": "A",
    "b": "B",
    "be": "B",
    "bee": "B",
    "bravo": "B",
    "c": "C",
    "see": "C",
    "sea": "C",
    "charlie": "C",
    "d": "D",
    "dee": "D",
    "delta": "D",
    "e": "E",
    "ee": "E",
    "echo": "E",
    "f": "F",
    "ef": "F",
    "foxtrot": "F",
    "g": "G",
    "gee": "G",
    "golf": "G",
    "h": "H",
    "aitch": "H",
    "ache": "H",
    "hotel": "H",
    "i": "I",
    "eye": "I",
    "india": "I",
    "j": "J",
    "jay": "J",
    "juliet": "J",
    "k": "K",
    "kay": "K",
    "kilo": "K",
    "l": "L",
    "el": "L",
    "ell": "L",
    "lima": "L",
    "m": "M",
    "em": "M",
    "mike": "M",
    "n": "N",
    "en": "N",
    "november": "N",
    "o": "O",
    "oh": "O",
    "oscar": "O",
    "p": "P",
    "pee": "P",
    "papa": "P",
    "q": "Q",
    "cue": "Q",
    "quebec": "Q",
    "r": "R",
    "are": "R",
    "ar": "R",
    "romeo": "R",
    "s": "S",
    "ess": "S",
    "as": "S",
    "sierra": "S",
    "t": "T",
    "tee": "T",
    "tea": "T",
    "tango": "T",
    "u": "U",
    "you": "U",
    "uniform": "U",
    "v": "V",
    "vee": "V",
    "victor": "V",
    "w": "W",
    "doubleu": "W",
    "whiskey": "W",
    "x": "X",
    "ex": "X",
    "xray": "X",
    "y": "Y",
    "why": "Y",
    "yankee": "Y",
    "z": "Z",
    "zed": "Z",
    "zee": "Z",
    "zulu": "Z",
}


def is_registration_offer_yes(user_text: str) -> bool:
    text = _strip_trailing_punct(user_text or "")
    if not text:
        return False
    if any(p.search(text) for p in _FRAMED_NAME_PATTERNS):
        return False
    return bool(_OFFER_YES_RE.match(text))


def is_registration_offer_no(user_text: str) -> bool:
    text = _strip_trailing_punct(user_text or "")
    if not text:
        return False
    if any(p.search(text) for p in _FRAMED_NAME_PATTERNS):
        return False
    return bool(_OFFER_NO_RE.match(text))


def is_confirm_yes(user_text: str) -> bool:
    text = _strip_trailing_punct(user_text or "")
    return bool(text) and bool(_CONFIRM_YES_RE.match(text))


def is_confirm_no(user_text: str) -> bool:
    text = _strip_trailing_punct(user_text or "")
    return bool(text) and bool(_CONFIRM_NO_RE.match(text))


def spell_name_aloud(name: str) -> str:
    letters = [ch.upper() for ch in (name or "") if ch.isalpha()]
    return ", ".join(letters) if letters else (name or "")


def parse_spelled_name(user_text: str) -> str | None:
    """Turn spoken spelling ('H A R I' / 'aitch ay ar eye') into a name."""
    text = _strip_trailing_punct(user_text or "")
    if not text:
        return None
    tokens = re.split(r"[\s,.\-/]+", text.lower())
    letters: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if not tok:
            i += 1
            continue
        if tok == "double" and i + 1 < len(tokens) and tokens[i + 1] in {"you", "u"}:
            letters.append("W")
            i += 2
            continue
        mapped = _LETTER_WORDS.get(tok)
        if mapped:
            letters.append(mapped)
            i += 1
            continue
        if len(tok) == 1 and tok.isalpha():
            letters.append(tok.upper())
            i += 1
            continue
        i += 1
    if len(letters) < 2:
        return None
    return "".join(letters).title()
