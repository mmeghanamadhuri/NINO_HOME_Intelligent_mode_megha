"""Local LLM via Ollama HTTP API (same pattern as voice_optimized assistant)."""

from __future__ import annotations

import os
from typing import Any

import requests

DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
DEFAULT_MODEL = "qwen2.5:1.5b"


def ollama_generate(
    prompt: str,
    *,
    model: str | None = None,
    api_url: str | None = None,
    timeout_s: int = 90,
    num_predict: int | None = 96,
) -> str:
    url = (api_url or os.environ.get("OLLAMA_URL") or DEFAULT_OLLAMA_URL).strip()
    m = (model or os.environ.get("OLLAMA_MODEL") or DEFAULT_MODEL).strip()
    payload: dict[str, Any] = {"model": m, "prompt": prompt, "stream": False}
    if num_predict is not None:
        payload["options"] = {"num_predict": num_predict}

    r = requests.post(url, json=payload, timeout=timeout_s)
    r.raise_for_status()
    data = r.json()
    text = str(data.get("response", "")).strip()
    if not text:
        raise RuntimeError("Ollama returned an empty response.")
    return text


def brief_spoken_message(
    situation: str,
    *,
    model: str | None = None,
    api_url: str | None = None,
) -> str:
    prompt = (
        "You are NiNO, a voice assistant.\n"
        f"Situation: {situation}\n"
        "Reply with exactly one short sentence the user will hear aloud. "
        "No quotes, no bullet points, no stage directions."
    )
    return ollama_generate(prompt, model=model, api_url=api_url, num_predict=64)


def greeting_for_face(
    display_name: str,
    *,
    is_return_visitor: bool,
    model: str | None = None,
    api_url: str | None = None,
    num_predict: int = 48,
    timeout_s: int = 25,
) -> str:
    visitor = (
        "They have been seen before this session (returning)."
        if is_return_visitor
        else "This is the first time they appear in front of the camera this session."
    )
    prompt = (
        f"You are NiNO, a friendly smart-home assistant with a camera.\n"
        f"The camera just recognized this person: {display_name}.\n"
        f"{visitor}\n"
        "Reply with 1–2 short spoken sentences only: warm greeting, use their name, "
        "offer help. No quotes, no bullet points, no stage directions."
    )
    return ollama_generate(
        prompt,
        model=model,
        api_url=api_url,
        timeout_s=timeout_s,
        num_predict=num_predict,
    )


def answer_voice_query(
    user_text: str,
    *,
    viewer_name: str | None = None,
    model: str | None = None,
    api_url: str | None = None,
    max_words: int = 40,
) -> str:
    if viewer_name:
        who = (
            f"You are speaking to {viewer_name.strip()}, identified by the home camera. "
            "This may be a follow-up question in the same conversation. "
            "On EVERY reply you must start by using their name in a brief, natural way "
            "(not only the first time), then answer what they asked. "
            "Never skip their name when they are identified. "
            "Example tone (do not copy verbatim): "
            f"\"Hi {viewer_name.strip()}, here is what you asked for — …\""
        )
    else:
        who = (
            "No registered person is clearly in front of the camera right now. "
            "Answer the question directly; do not invent or guess a name."
        )

    prompt = (
        "You are NiNO, a concise voice assistant for a smart home with a camera.\n"
        f"{who}\n"
        f"Rules: one short spoken reply under {max_words} words, plain sentences, "
        "no lists, no markdown, no stage directions, suitable to read aloud.\n"
        f"The user asked: {user_text}"
    )
    return ollama_generate(
        prompt,
        model=model,
        api_url=api_url,
        num_predict=192,
    )
