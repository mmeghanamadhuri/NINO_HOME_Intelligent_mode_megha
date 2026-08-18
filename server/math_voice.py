"""Deterministic spoken arithmetic for voice queries."""

from __future__ import annotations

import random
import re
import threading
from dataclasses import dataclass
from typing import Literal

MathOp = Literal["add", "subtract", "multiply", "divide"]

_NUMBER_WORDS: dict[str, int] = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
    "hundred": 100,
    "thousand": 1000,
}

_OP_WORDS: dict[str, MathOp] = {
    "plus": "add",
    "add": "add",
    "added": "add",
    "addition": "add",
    "minus": "subtract",
    "subtract": "subtract",
    "subtraction": "subtract",
    "take away": "subtract",
    "times": "multiply",
    "multiply": "multiply",
    "multiplied": "multiply",
    "multiplication": "multiply",
    "divided": "divide",
    "divide": "divide",
    "division": "divide",
    "over": "divide",
}

_EXPLICIT_EXPR = re.compile(
    r"^\s*(?P<a>[a-z0-9]+)\s+"
    r"(?P<op>plus|minus|times|multiplied\s+by|divided\s+by|over|add(?:ed)?(?:\s+to)?)\s+"
    r"(?P<b>[a-z0-9]+)\s*[.!?…]*\s*$",
    re.IGNORECASE,
)

_PAIR_EXPR = re.compile(
    r"^\s*(?P<a>[a-z0-9]+)\s+(?:and|,|with)\s+(?P<b>[a-z0-9]+)\s*[.!?…]*\s*$",
    re.IGNORECASE,
)

_SYMBOL_EXPR = re.compile(
    r"^\s*(?P<a>\d+)\s*([+\-*/x×])\s*(?P<b>\d+)\s*[.!?…]*\s*$",
    re.IGNORECASE,
)

_QUIZ_ME_RE = re.compile(
    r"\b(?:"
    r"quiz me|"
    r"ask me(?:\s+some)?\s+questions?|"
    r"you (?:give|ask) me(?:\s+(?:a|some|two))?\s+(?:numbers?|problems?|questions?)|"
    r"give me(?:\s+(?:a|some|two))?\s+(?:numbers?|problems?|questions?|a sum)"
    r")\b",
    re.IGNORECASE,
)
_LEARN_OP_RE = re.compile(
    r"\b(?:learn|practice|practise|study)\s+(?:some\s+)?"
    r"(?P<op>additions?|multiplications?|subtractions?|divisions?)\b",
    re.IGNORECASE,
)
_DONT_KNOW_RE = re.compile(
    r"\b(?:i\s+don'?t\s+know(?:\s+that)?|no\s+idea|not\s+sure|idk)\b",
    re.IGNORECASE,
)
_CONTINUE_RE = re.compile(
    r"^\s*(?:yeah|yes|yep|yup|ok|okay|sure|go on|next|another|again)\s*[.!?…]*\s*$",
    re.IGNORECASE,
)
_STOP_QUIZ_RE = re.compile(
    r"\b(?:stop(?:\s+the)?\s+(?:quiz|practice|math)|enough math|that'?s enough)\b",
    re.IGNORECASE,
)
_ANSWER_PREFIX_RE = re.compile(
    r"^\s*(?:that'?s|it'?s|the answer is|answer is|i\s+(?:think|guess)(?:\s+it'?s)?)\s*",
    re.IGNORECASE,
)
_THINK_NUMBER_RE = re.compile(
    r"\b(?:i\s+(?:think|guess)|that'?s|it'?s|the answer is)\s+(-?\d+)\s*[.!?…]*\s*$",
    re.IGNORECASE,
)
_STILL_ON_PROBLEM_RE = re.compile(
    r"\b(?:didn'?t|did not|haven'?t|have not)\s+answer",
    re.IGNORECASE,
)
_FIRST_SECOND_RE = re.compile(
    r"first number[:\s]+(\d+).{0,80}?second number[:\s]+(\d+)",
    re.IGNORECASE | re.DOTALL,
)
_WHAT_IS_SPOKEN_RE = re.compile(
    r"what is\s+([a-z0-9]+)\s+(plus|minus|times|divided by)\s+([a-z0-9]+)",
    re.IGNORECASE,
)

_quiz_lock = threading.Lock()
_quizzes: dict[str, "MathQuiz"] = {}


@dataclass(frozen=True)
class MathResult:
    left: int
    right: int
    operation: MathOp
    result: int


@dataclass(frozen=True)
class MathQuiz:
    operation: MathOp
    left: int
    right: int
    answer: int


def _parse_number_token(raw: str) -> int | None:
    token = str(raw or "").strip().lower()
    if not token:
        return None
    if token.isdigit():
        return int(token)
    return _NUMBER_WORDS.get(token)


def _normalize_op(raw: str) -> MathOp | None:
    cleaned = re.sub(r"\s+", " ", str(raw or "").strip().lower())
    if cleaned in _OP_WORDS:
        return _OP_WORDS[cleaned]
    if cleaned in {"multiplied by", "divided by"}:
        return "multiply" if "multiplied" in cleaned else "divide"
    return None


def _compute(left: int, right: int, operation: MathOp) -> int | None:
    if operation == "add":
        return left + right
    if operation == "subtract":
        return left - right
    if operation == "multiply":
        return left * right
    if operation == "divide":
        if right == 0:
            return None
        if left % right != 0:
            return None
        return left // right
    return None


def infer_session_math_operation(
    session_turns: list[tuple[str, str]] | None,
) -> MathOp | None:
    """Guess add/subtract/multiply/divide from recent session wording (newest first)."""
    if not session_turns:
        return None

    def _detect(text: str) -> MathOp | None:
        blob = text.lower()
        if re.search(r"\b(?:divide|division|divisions|dividing)\b", blob):
            return "divide"
        if re.search(r"\b(?:multiply|multiplication|multiplying)\b", blob):
            return "multiply"
        if re.search(r"\b(?:subtract|subtraction|minus|take away)\b", blob):
            return "subtract"
        if re.search(r"\b(?:add|addition|additions|adding|plus)\b", blob):
            return "add"
        return None

    for user_text, assistant_text in reversed(session_turns[-5:]):
        for chunk in (user_text, assistant_text):
            op = _detect(chunk)
            if op:
                return op
    return None


def parse_spoken_math(
    user_text: str,
    *,
    session_turns: list[tuple[str, str]] | None = None,
) -> MathResult | None:
    """Parse a spoken math expression, optionally using session operation for 'X and Y'."""
    text = str(user_text or "").strip()
    if not text:
        return None

    symbol = _SYMBOL_EXPR.match(text)
    if symbol:
        left = int(symbol.group("a"))
        right = int(symbol.group("b"))
        sym = symbol.group(2)
        op: MathOp
        if sym in {"+", "plus"}:
            op = "add"
        elif sym == "-":
            op = "subtract"
        elif sym in {"*", "x", "×"}:
            op = "multiply"
        else:
            op = "divide"
        result = _compute(left, right, op)
        if result is None:
            return None
        return MathResult(left=left, right=right, operation=op, result=result)

    explicit = _EXPLICIT_EXPR.match(text)
    if explicit:
        left = _parse_number_token(explicit.group("a"))
        right = _parse_number_token(explicit.group("b"))
        op = _normalize_op(explicit.group("op"))
        if left is None or right is None or op is None:
            return None
        result = _compute(left, right, op)
        if result is None:
            return None
        return MathResult(left=left, right=right, operation=op, result=result)

    pair = _PAIR_EXPR.match(text)
    if pair:
        left = _parse_number_token(pair.group("a"))
        right = _parse_number_token(pair.group("b"))
        op = infer_session_math_operation(session_turns)
        if left is None or right is None or op is None:
            return None
        result = _compute(left, right, op)
        if result is None:
            return None
        return MathResult(left=left, right=right, operation=op, result=result)

    return None


def _spoken_number(value: int) -> str:
    for word, number in _NUMBER_WORDS.items():
        if number == value and word not in {"zero", "hundred", "thousand"}:
            return word
    return str(value)


def _operation_phrase(operation: MathOp) -> str:
    return {
        "add": "plus",
        "subtract": "minus",
        "multiply": "times",
        "divide": "divided by",
    }[operation]


def format_math_reply(result: MathResult) -> str:
    left = _spoken_number(result.left)
    right = _spoken_number(result.right)
    op = _operation_phrase(result.operation)
    answer = _spoken_number(result.result)
    if result.operation == "divide":
        return f"{left.capitalize()} {op} {right} is {answer}."
    return f"{left.capitalize()} {op} {right} is {answer}."


def try_spoken_math_reply(
    user_text: str,
    *,
    session_turns: list[tuple[str, str]] | None = None,
) -> str | None:
    parsed = parse_spoken_math(user_text, session_turns=session_turns)
    if not parsed:
        return None
    return format_math_reply(parsed)


def _quiz_key(device_id: str | None) -> str:
    return str(device_id or "").strip() or "__default__"


def clear_math_quiz(device_id: str | None) -> None:
    with _quiz_lock:
        _quizzes.pop(_quiz_key(device_id), None)


def get_math_quiz(device_id: str | None) -> MathQuiz | None:
    with _quiz_lock:
        return _quizzes.get(_quiz_key(device_id))


def _set_math_quiz(device_id: str | None, quiz: MathQuiz) -> None:
    with _quiz_lock:
        _quizzes[_quiz_key(device_id)] = quiz


def _op_from_learn_text(user_text: str) -> MathOp | None:
    match = _LEARN_OP_RE.search(user_text)
    if not match:
        return None
    return infer_session_math_operation([(match.group("op"), "")])


def wants_math_quiz(
    user_text: str,
    *,
    session_turns: list[tuple[str, str]] | None = None,
) -> bool:
    text = str(user_text or "").strip()
    if not text:
        return False
    if _LEARN_OP_RE.search(text):
        return True
    if re.search(
        r"\b(?:can you\s+)?(?:you give me|give me)(?:\s+(?:a|some|two))?\s+numbers?\b",
        text,
        re.IGNORECASE,
    ):
        return True
    if re.search(r"\bquiz me\b", text, re.IGNORECASE):
        return True
    if _QUIZ_ME_RE.search(text) and (
        infer_session_math_operation(session_turns)
        or infer_session_math_operation([(text, "")])
    ):
        return True
    return False


def _parse_answer_number(user_text: str) -> int | None:
    text = str(user_text or "").strip()
    if not text or _STILL_ON_PROBLEM_RE.search(text):
        return None
    think = _THINK_NUMBER_RE.search(text)
    if think:
        return int(think.group(1))
    cleaned = _ANSWER_PREFIX_RE.sub("", text)
    cleaned = cleaned.strip().rstrip(".!?…").strip()
    if not cleaned:
        return None
    if cleaned.lstrip("-").isdigit():
        return int(cleaned)
    return _parse_number_token(cleaned)


def _quiz_from_numbers(
    left: int,
    right: int,
    operation: MathOp,
) -> MathQuiz | None:
    result = _compute(left, right, operation)
    if result is None:
        return None
    return MathQuiz(operation=operation, left=left, right=right, answer=result)


def _quiz_from_assistant_text(
    assistant_text: str,
    operation: MathOp,
) -> MathQuiz | None:
    blob = str(assistant_text or "").strip()
    if not blob:
        return None
    spoken = _WHAT_IS_SPOKEN_RE.search(blob)
    if spoken:
        left = _parse_number_token(spoken.group(1))
        right = _parse_number_token(spoken.group(3))
        op = _normalize_op(spoken.group(2)) or operation
        if left is not None and right is not None:
            return _quiz_from_numbers(left, right, op)
    pair = _FIRST_SECOND_RE.search(blob)
    if pair:
        return _quiz_from_numbers(int(pair.group(1)), int(pair.group(2)), operation)
    and_pair = re.search(r"\b(\d+)\s+and\s+(\d+)\b", blob)
    if and_pair and re.search(r"\b(?:sum|add|plus|times|multiply)\b", blob, re.I):
        return _quiz_from_numbers(
            int(and_pair.group(1)), int(and_pair.group(2)), operation
        )
    return None


def _recover_quiz_from_session(
    session_turns: list[tuple[str, str]] | None,
) -> MathQuiz | None:
    if not session_turns:
        return None
    operation = infer_session_math_operation(session_turns) or "add"
    for _user_text, assistant_text in reversed(session_turns[-6:]):
        quiz = _quiz_from_assistant_text(assistant_text, operation)
        if quiz:
            return quiz
    return None


def _new_quiz_problem(
    operation: MathOp,
    *,
    avoid: MathQuiz | None = None,
) -> MathQuiz:
    for _ in range(12):
        if operation == "subtract":
            right = random.randint(1, 9)
            left = random.randint(right, right + 9)
        elif operation == "multiply":
            left = random.randint(2, 9)
            right = random.randint(2, 9)
        elif operation == "divide":
            right = random.randint(2, 9)
            left = right * random.randint(2, 9)
        else:
            left = random.randint(2, 12)
            right = random.randint(2, 12)
            operation = "add"
        result = _compute(left, right, operation)
        if result is None:
            continue
        quiz = MathQuiz(
            operation=operation, left=left, right=right, answer=result
        )
        if avoid is None or quiz != avoid:
            return quiz
    return MathQuiz(operation="add", left=3, right=4, answer=7)


def _ask_quiz_question(quiz: MathQuiz, *, lead: str = "") -> str:
    left = _spoken_number(quiz.left)
    right = _spoken_number(quiz.right)
    op = _operation_phrase(quiz.operation)
    question = f"What is {left} {op} {right}?"
    lead = str(lead or "").strip()
    if lead:
        if lead[-1] not in ".!?":
            lead = f"{lead}."
        return f"{lead} {question}"
    return question


def try_math_quiz_reply(
    user_text: str,
    *,
    device_id: str | None = None,
    session_turns: list[tuple[str, str]] | None = None,
) -> str | None:
    """Pose and grade a spoken math quiz when the user asked to be asked."""
    text = str(user_text or "").strip()
    if not text:
        return None

    if _STOP_QUIZ_RE.search(text):
        if get_math_quiz(device_id) is None:
            return None
        clear_math_quiz(device_id)
        return "Okay, we'll stop the practice."

    current = get_math_quiz(device_id)
    if current is None:
        recovered = _recover_quiz_from_session(session_turns)
        if recovered:
            _set_math_quiz(device_id, recovered)
            current = recovered

    start_quiz = wants_math_quiz(text, session_turns=session_turns)
    if start_quiz:
        operation = (
            _op_from_learn_text(text)
            or infer_session_math_operation(session_turns)
            or infer_session_math_operation([(text, "")])
            or (current.operation if current else None)
            or "add"
        )
        quiz = _new_quiz_problem(operation, avoid=current)
        _set_math_quiz(device_id, quiz)
        lead = "Okay, your turn." if current else "Okay."
        return _ask_quiz_question(quiz, lead=lead)

    if current is None:
        return None

    if _STILL_ON_PROBLEM_RE.search(text):
        return _ask_quiz_question(current, lead="Okay, let's stay on this one.")

    if _DONT_KNOW_RE.search(text):
        known = format_math_reply(
            MathResult(
                left=current.left,
                right=current.right,
                operation=current.operation,
                result=current.answer,
            )
        )
        nxt = _new_quiz_problem(current.operation, avoid=current)
        _set_math_quiz(device_id, nxt)
        return _ask_quiz_question(nxt, lead=known + " Next one.")

    if _CONTINUE_RE.match(text):
        nxt = _new_quiz_problem(current.operation, avoid=current)
        _set_math_quiz(device_id, nxt)
        return _ask_quiz_question(nxt, lead="Okay.")

    guessed = _parse_answer_number(text)
    if guessed is None:
        if len(text.split()) <= 3:
            return _ask_quiz_question(current, lead="I didn't catch the answer.")
        return None

    nxt = _new_quiz_problem(current.operation, avoid=current)
    _set_math_quiz(device_id, nxt)
    if guessed == current.answer:
        return _ask_quiz_question(nxt, lead="That's right.")
    known = format_math_reply(
        MathResult(
            left=current.left,
            right=current.right,
            operation=current.operation,
            result=current.answer,
        )
    )
    return _ask_quiz_question(nxt, lead=f"Not quite. {known} Try this.")
