"""Deterministic spoken arithmetic for voice queries."""

from __future__ import annotations

import re
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


@dataclass(frozen=True)
class MathResult:
    left: int
    right: int
    operation: MathOp
    result: int


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
