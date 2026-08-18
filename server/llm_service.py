"""Local LLM via Ollama HTTP API (same pattern as voice_optimized assistant)."""

from __future__ import annotations

import json
import logging
import os
import random
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any

import requests

logger = logging.getLogger(__name__)

DEFAULT_OLLAMA_GPU_URL = "http://127.0.0.1:11435/api/generate"
DEFAULT_OLLAMA_CPU_URL = "http://127.0.0.1:11434/api/generate"
DEFAULT_OLLAMA_URL = DEFAULT_OLLAMA_GPU_URL
DEFAULT_MODEL = "qwen2.5:1.5b"
DEFAULT_KEEP_ALIVE = "30m"
DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11435"
# CPU fallback can exceed 90s; GPU replies are typically under 5s.
VOICE_QUERY_TIMEOUT_S = 60
OLLAMA_UNAVAILABLE_REPLY = (
    "Sorry, I could not reach the language model in time. Please try again."
)


def resolve_ollama_api_url(
    *,
    model: str | None = None,
    preferred: str | None = None,
) -> str:
    """Pick the best Ollama endpoint, preferring the GPU instance on :11435."""
    explicit = (preferred or os.environ.get("OLLAMA_URL") or "").strip()
    m = (model or os.environ.get("OLLAMA_MODEL") or DEFAULT_MODEL).strip()
    if explicit and explicit.lower() not in {"auto", "detect"}:
        gpu_url = os.environ.get("OLLAMA_GPU_URL", DEFAULT_OLLAMA_GPU_URL).strip()
        if (
            gpu_url
            and gpu_url != explicit
            and not ollama_model_available(model=m, api_url=explicit)
            and ollama_model_available(model=m, api_url=gpu_url)
        ):
            logger.warning(
                "Configured Ollama endpoint %s does not provide model %s; using %s instead",
                explicit,
                m,
                gpu_url,
            )
            return gpu_url
        return explicit

    candidates = [
        os.environ.get("OLLAMA_GPU_URL", DEFAULT_OLLAMA_GPU_URL).strip(),
        DEFAULT_OLLAMA_CPU_URL,
    ]
    seen: set[str] = set()
    ordered = [u for u in candidates if u and u not in seen and not seen.add(u)]

    best_reachable = ""
    for api_url in ordered:
        status = ollama_runtime_status(model=m, api_url=api_url)
        if not status.get("reachable"):
            continue
        if status.get("on_gpu"):
            logger.info(
                "Using GPU Ollama at %s (%s, size_vram=%s)",
                api_url,
                status.get("processor") or "gpu",
                status.get("size_vram"),
            )
            return api_url
        if not best_reachable:
            best_reachable = api_url

    if best_reachable:
        status = ollama_runtime_status(model=m, api_url=best_reachable)
        logger.warning(
            "Using CPU Ollama at %s. Start GPU Ollama: bash server/scripts/start_ollama_gpu.sh — %s",
            best_reachable,
            status.get("warning") or "",
        )
        return best_reachable

    logger.warning(
        "No Ollama reachable; defaulting to %s. Run: bash server/scripts/start_ollama_gpu.sh",
        DEFAULT_OLLAMA_GPU_URL,
    )
    return DEFAULT_OLLAMA_GPU_URL


def try_start_gpu_ollama() -> None:
    """Start user-local GPU Ollama if installed and not already listening."""
    install_bin = os.path.join(
        os.environ.get("OLLAMA_GPU_HOME", os.path.expanduser("~/.local/ollama-gpu")),
        "bin",
        "ollama",
    )
    if not os.path.isfile(install_bin):
        return
    gpu_base = _ollama_base_url(os.environ.get("OLLAMA_GPU_URL", DEFAULT_OLLAMA_GPU_URL))
    try:
        requests.get(f"{gpu_base}/api/tags", timeout=2)
        return
    except Exception:
        pass
    script = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "scripts",
        "start_ollama_gpu.sh",
    )
    if not os.path.isfile(script):
        return
    logger.info("Starting local GPU Ollama via %s", script)
    subprocess.run(["bash", script], check=False, timeout=120)


def _normalize_ollama_generate_url(url: str) -> str:
    """Ensure POST target is .../api/generate (not the server root)."""
    raw = url.strip().rstrip("/")
    if not raw:
        return DEFAULT_OLLAMA_URL
    if raw.endswith("/api/generate") or raw.endswith("/api/chat"):
        return raw
    return f"{raw}/api/generate"


def _ollama_base_url(api_url: str | None = None) -> str:
    raw = (api_url or os.environ.get("OLLAMA_URL") or DEFAULT_OLLAMA_URL).strip()
    if raw.endswith("/api/generate"):
        return raw[: -len("/api/generate")]
    if raw.endswith("/api/chat"):
        return raw[: -len("/api/chat")]
    return raw.rstrip("/")


def ollama_model_available(*, model: str, api_url: str, timeout_s: int = 3) -> bool:
    """Return whether an Ollama endpoint has the requested model installed."""
    wanted = model.strip()
    if not wanted:
        return False
    names = {wanted}
    if ":" not in wanted:
        names.add(f"{wanted}:latest")
    try:
        response = requests.get(
            f"{_ollama_base_url(api_url)}/api/tags",
            timeout=timeout_s,
        )
        response.raise_for_status()
        models = response.json().get("models", [])
        return any(
            str(entry.get("name") or entry.get("model") or "").strip() in names
            for entry in models
            if isinstance(entry, dict)
        )
    except (requests.RequestException, TypeError, ValueError):
        return False


def _ollama_cli_binary() -> str:
    user_bin = os.path.join(
        os.environ.get("OLLAMA_GPU_HOME", os.path.expanduser("~/.local/ollama-gpu")),
        "bin",
        "ollama",
    )
    if os.path.isfile(user_bin):
        return user_bin
    return shutil.which("ollama") or ""


def _ollama_cli_processor(model: str, *, base_url: str = "") -> str:
    ollama_bin = _ollama_cli_binary()
    if not ollama_bin:
        return ""
    env = os.environ.copy()
    if base_url:
        env["OLLAMA_HOST"] = base_url.replace("https://", "").replace("http://", "")
    try:
        completed = subprocess.run(
            [ollama_bin, "ps"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env=env,
        )
    except Exception:
        return ""
    for line in completed.stdout.splitlines()[1:]:
        if not line.startswith(model):
            continue
        match = re.search(
            r"(\d+%\s*(?:CPU|GPU)(?:\s*/\s*\d+%\s*(?:CPU|GPU))?)",
            line,
        )
        return match.group(1) if match else ""
    return ""


def ollama_runtime_status(
    *,
    model: str | None = None,
    api_url: str | None = None,
    timeout_s: int = 5,
) -> dict[str, Any]:
    """Inspect Ollama /api/ps to see whether the model is on GPU or CPU."""
    m = (model or os.environ.get("OLLAMA_MODEL") or DEFAULT_MODEL).strip()
    base = _ollama_base_url(api_url)
    out: dict[str, Any] = {
        "model": m,
        "base_url": base,
        "reachable": False,
        "loaded": False,
        "processor": "",
        "size_vram": 0,
        "on_gpu": False,
        "warning": "",
    }
    try:
        r = requests.get(f"{base}/api/ps", timeout=timeout_s)
        r.raise_for_status()
        out["reachable"] = True
        loaded_models: list[str] = []
        for entry in r.json().get("models", []):
            name = str(entry.get("name", ""))
            if name:
                loaded_models.append(name)
            if name != m:
                continue
            out["loaded"] = True
            out["size_vram"] = int(entry.get("size_vram") or 0)
            break
        out["processor"] = _ollama_cli_processor(m, base_url=base)
        proc = out["processor"].upper()
        if out["size_vram"] > 0:
            out["on_gpu"] = True
        elif proc:
            out["on_gpu"] = "GPU" in proc and "100% CPU" not in proc
        else:
            out["on_gpu"] = False
        if not out["on_gpu"] and (not proc or "CPU" in proc):
            out["warning"] = (
                "Ollama is running on CPU only (snap builds do this on GB10). "
                "Install GPU Ollama: sudo bash server/scripts/setup_ollama_gpu.sh"
            )
        elif proc and "GPU" in proc and "CPU" in proc:
            out["warning"] = (
                "Ollama is using a mixed CPU/GPU split. Unload and reload the model "
                "after freeing unified memory."
            )
        extra = [name for name in loaded_models if name != m]
        if extra:
            out["other_loaded_models"] = extra
            out["warning"] = (
                (out["warning"] + " " if out["warning"] else "")
                + f"Other models are also loaded ({', '.join(extra)}), which can slow inference."
            ).strip()
        if out["reachable"] and not out["loaded"]:
            out["warning"] = "Model is not loaded yet; first voice query may be slow."
    except Exception as exc:
        out["warning"] = f"Ollama status check failed: {exc}"
    return out


def is_ollama_error(exc: BaseException) -> bool:
    if isinstance(exc, requests.RequestException):
        return True
    if isinstance(exc, RuntimeError) and "ollama" in str(exc).lower():
        return True
    return False


def warm_ollama_model(
    *,
    model: str | None = None,
    api_url: str | None = None,
    timeout_s: int = VOICE_QUERY_TIMEOUT_S,
) -> bool:
    """Load the configured model into Ollama memory before the first user request."""
    m = (model or os.environ.get("OLLAMA_MODEL") or DEFAULT_MODEL).strip()
    try:
        ollama_generate(
            "Hi",
            model=m,
            api_url=api_url,
            timeout_s=timeout_s,
            num_predict=8,
        )
        status = ollama_runtime_status(model=m, api_url=api_url)
        if status.get("on_gpu"):
            logger.info(
                "Ollama model %s warmed on GPU (%s, size_vram=%s)",
                m,
                status.get("processor"),
                status.get("size_vram"),
            )
        else:
            logger.warning(
                "Ollama model %s warmed but not on GPU (%s). %s",
                m,
                status.get("processor") or "not loaded",
                status.get("warning") or "Run server/scripts/setup_ollama_gpu.sh",
            )
        return True
    except Exception as exc:
        logger.warning("Ollama warmup failed for %s: %s", m, exc)
        return False


def ollama_generate(
    prompt: str,
    *,
    model: str | None = None,
    api_url: str | None = None,
    timeout_s: int = 90,
    num_predict: int | None = 96,
    keep_alive: str | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
) -> str:
    url = _normalize_ollama_generate_url(
        (api_url or os.environ.get("OLLAMA_URL") or DEFAULT_OLLAMA_URL).strip()
    )
    m = (model or os.environ.get("OLLAMA_MODEL") or DEFAULT_MODEL).strip()
    alive = (
        keep_alive
        if keep_alive is not None
        else os.environ.get("OLLAMA_KEEP_ALIVE", DEFAULT_KEEP_ALIVE)
    ).strip()
    payload: dict[str, Any] = {
        "model": m,
        "prompt": prompt,
        "stream": False,
        "keep_alive": alive,
    }
    options: dict[str, Any] = {}
    if num_predict is not None:
        options["num_predict"] = num_predict
    if temperature is not None:
        options["temperature"] = float(max(0.0, min(2.0, temperature)))
    if top_p is not None:
        options["top_p"] = float(max(0.05, min(1.0, top_p)))
    if os.environ.get("OLLAMA_FORCE_GPU", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }:
        raw_gpu = os.environ.get("OLLAMA_NUM_GPU", "-1").strip()
        try:
            options["num_gpu"] = int(raw_gpu)
        except ValueError:
            options["num_gpu"] = -1
    if options:
        payload["options"] = options

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


def build_greeting_prompt(
    display_name: str,
    *,
    is_return_visitor: bool,
    session_summary: str | None = None,
    is_startup_greeting: bool = False,
) -> str:
    """Prompt for camera face greeting; optional Phase C summary from a prior day."""
    visitor = (
        "They have been seen before this session (returning)."
        if is_return_visitor
        else "This is the first time they appear in front of the camera this session."
    )
    lines = [
        "You are NiNO, a friendly smart-home assistant with a camera.",
        f"The camera just recognized this person: {display_name}.",
        visitor,
    ]
    summary = (session_summary or "").strip()
    if summary:
        if is_startup_greeting:
            lines.append(
                "This is the server's first welcome greeting after boot. "
                "Use the prior-day summary below. Do NOT mention facial expression, "
                "mood, or how they look — only greet and reference yesterday's topics."
            )
        lines.append(
            "Earlier session summary (from a prior day — weave in naturally, do not read verbatim):\n"
            f"{summary}"
        )
        lines.append(
            "Reply with 2 short spoken sentences only (max 35 words total): warm greeting using their name, "
            "mention exactly ONE topic from the summary, then ask if they want to continue THAT SAME topic. "
            "Do not reference a second, different topic. "
            "No quotes, no bullet points, no stage directions."
        )
    else:
        lines.append(
            "Reply with 1–2 short spoken sentences only: warm greeting, use their name, "
            "offer help. No quotes, no bullet points, no stage directions."
        )
    return "\n".join(lines)


_SUMMARY_PREFERENCE_RE = re.compile(
    r"\b("
    r"preferred|prefer(?:s|red)?|favourite|favorite|"
    r"coffee|tea|chess|gaming preferences|outdoor games|"
    r"birthday|birthdate|alarm|reminder"
    r")\b",
    re.IGNORECASE,
)

_SUMMARY_TOPIC_PREFIX_RE = re.compile(
    r"^(?:user\s+)?(?:"
    r"learned about|received names of|received|discussed|talked about|"
    r"asked about|explored|covered|conversation shifted to discussing"
    r")\s+",
    re.IGNORECASE,
)

# Markdown headings / section labels are not speakable topics
# (e.g. "### Summary" was spoken as "Yesterday we discussed ### Summary").
_SUMMARY_HEADING_RE = re.compile(r"^#{1,6}\s*")
_SUMMARY_SECTION_LABEL_RE = re.compile(
    r"^(?:summary|overview|highlights?|topics?|notes?|recap|"
    r"session\s+summary|daily\s+summary|key\s+points?)\s*$",
    re.IGNORECASE,
)
# Strip "1." / "2)" / "(3)" before clause-splitting — otherwise "1. Mars" → topic "1".
_SUMMARY_LIST_NUMBER_RE = re.compile(r"^(?:\(?\d+\)?[.)]\s+|\d+\s+[-–—]\s+)")
_SUMMARY_JUNK_TOPIC_RE = re.compile(
    r"^(?:\d+|topic\s*\d+|item\s*\d+|point\s*\d+)\s*$",
    re.IGNORECASE,
)


def parse_summary_topics_for_greeting(
    summary_text: str, *, max_topics: int = 2
) -> list[str]:
    """Extract speakable topic phrases from a Phase C summary; skip personal prefs."""
    topics: list[str] = []
    for raw_line in summary_text.splitlines():
        line = raw_line.strip()
        line = _SUMMARY_HEADING_RE.sub("", line)
        line = line.lstrip("-•*").strip()
        line = _SUMMARY_LIST_NUMBER_RE.sub("", line).strip()
        if not line or _SUMMARY_SECTION_LABEL_RE.match(line):
            continue
        if _SUMMARY_PREFERENCE_RE.search(line):
            continue
        if re.search(r"\bshifted to discussing\b", line, re.I):
            line = re.sub(
                r"^.*?\bshifted to discussing\s+",
                "",
                line,
                flags=re.IGNORECASE,
            )
        line = _SUMMARY_TOPIC_PREFIX_RE.sub("", line).strip()
        # Keep only the first clause so a topic is a single subject, never a
        # run-on with a second topic (which caused mixed greetings).
        line = re.split(r"[.;:]", line, maxsplit=1)[0].strip()
        line = line.rstrip(".")
        if not line or _SUMMARY_SECTION_LABEL_RE.match(line):
            continue
        if _SUMMARY_JUNK_TOPIC_RE.match(line) or len(line) < 3:
            continue
        if len(line) > 48:
            line = line[:45].rsplit(" ", 1)[0]
        if _SUMMARY_JUNK_TOPIC_RE.match(line) or len(line) < 3:
            continue
        topics.append(line)
        if len(topics) >= max_topics:
            break
    return topics


@dataclass(frozen=True)
class StartupGreetingParts:
    """Three-part startup greeting: hello → yesterday context → counter-question."""

    hello: str
    yesterday: str
    question: str

    def spoken(self) -> str:
        return f"{self.hello} {self.yesterday} {self.question}"


def build_startup_greeting_hello(display_name: str) -> str:
    return f"Hi {display_name.strip()}, good to see you!"


def build_startup_greeting_yesterday(primary_topic: str) -> str:
    topic = primary_topic.strip().rstrip(".")
    return f"Yesterday we discussed {topic}."


def build_startup_greeting_opener(display_name: str, primary_topic: str) -> str:
    """Fixed spoken opener: hello + yesterday context."""
    return (
        f"{build_startup_greeting_hello(display_name)} "
        f"{build_startup_greeting_yesterday(primary_topic)}"
    )


_GREETING_LEAK_RE = re.compile(
    r"^(?:hi\s+[^,.!?]+[,!]?\s*)?(?:good to see you[,!]?\s*)+",
    re.IGNORECASE,
)


def _sanitize_closing_question(question: str, display_name: str) -> str:
    """Keep only sentence 3 — strip greeting leaks from the LLM."""
    q = question.strip().strip("\"'")
    if not q:
        return q
    for segment in re.split(r"(?<=[.!?])\s+", q):
        seg = segment.strip()
        if seg.endswith("?"):
            q = seg
    q = _GREETING_LEAK_RE.sub("", q).strip()
    name = display_name.strip()
    if name and re.match(rf"^hi\s+{re.escape(name)}\b", q, re.I):
        q = re.sub(rf"^hi\s+{re.escape(name)}\s*[,!]?\s*", "", q, flags=re.I).strip()
    lower = q.lower()
    if "yesterday we discussed" in lower or "good to see you" in lower:
        q_pos = q.rfind("?")
        if q_pos > 0:
            start = q.rfind(".", 0, q_pos)
            if start >= 0:
                q = q[start + 1 : q_pos + 1].strip()
    if q and not q.endswith("?"):
        q = q.rstrip(".! ") + "?"
    return q


def build_startup_closing_question_prompt(
    display_name: str,
    primary_topic: str,
    hello: str,
    yesterday: str,
    summary_text: str,
) -> str:
    """LLM prompt for sentence 3 only — after hello and yesterday context were spoken.

    The closing question MUST stay on the same topic as sentence 2. We do not
    pass the full summary here — that previously caused the model to drift to a
    different topic (e.g. yesterday 'API' but the question asked about
    'microcontroller').
    """
    name = display_name.strip()
    topic = primary_topic.strip()
    return (
        "You are NiNO, a friendly voice assistant.\n"
        f"You already said aloud to {name} (in order):\n"
        f'1) "{hello}"\n'
        f'2) "{yesterday}"\n\n'
        f"Write ONLY line 3 — one short counter-question strictly about the SAME topic: {topic}.\n"
        "The listener already heard the greeting and yesterday's topic.\n"
        "Pick ONE style (vary between startups):\n"
        'A) Invite to continue that same topic (e.g. "Want to pick up where we left off?")\n'
        f'B) Ask a simple follow-up about {topic} (e.g. "Can you tell me more about {topic}?")\n\n'
        "Rules:\n"
        f"- The question MUST be about {topic} and nothing else. Never mention any other subject.\n"
        "- Output ONLY the question — no hi, no name, no \"good to see you\", no \"yesterday\"\n"
        "- Exactly one sentence ending with ?\n"
        "- Max 12 words, casual spoken English\n"
        "- No quotes, bullet points, or stage directions\n"
        f"\nTopic: {topic}\n"
    )


def _startup_greeting_temperature() -> float:
    temp_raw = os.environ.get("STARTUP_GREETING_TEMPERATURE")
    if temp_raw is None:
        temp_raw = os.environ.get("VOICE_REPLY_TEMPERATURE", "0.72")
    return float(temp_raw)


def _startup_closing_question_via_llm(
    display_name: str,
    primary_topic: str,
    hello: str,
    yesterday: str,
    summary_text: str,
    *,
    model: str | None = None,
    api_url: str | None = None,
    timeout_s: int = VOICE_QUERY_TIMEOUT_S,
) -> str:
    """LLM sentence 3: counter-question only (no greeting or yesterday recap)."""
    prompt = build_startup_closing_question_prompt(
        display_name, primary_topic, hello, yesterday, summary_text
    )
    question = ollama_generate(
        prompt,
        model=model,
        api_url=api_url,
        timeout_s=timeout_s,
        num_predict=48,
        temperature=_startup_greeting_temperature(),
    ).strip()
    return _sanitize_closing_question(question, display_name)


def build_startup_summary_greeting_prompt(
    display_name: str, summary_text: str
) -> str:
    """Fallback LLM prompt when topic extraction fails — full 3-part greeting."""
    name = display_name.strip()
    summary = summary_text.strip()
    topics = parse_summary_topics_for_greeting(summary)
    the_topic = topics[0] if topics else ""
    lines = [
        "You are NiNO, a friendly smart-home assistant with a camera.",
        f"The camera just recognized {name} — first welcome after the server started today.",
        "Write ONE short spoken reply they will hear aloud.",
        "",
        "Use EXACTLY this 3-sentence shape (fresh wording, same structure):",
        f'1) "Hi {name}, good to see you!"',
        '2) "Yesterday we discussed [ONE topic]."',
        "3) A question — either invite to continue OR ask a simple follow-up about THAT SAME topic.",
        "",
        "Rules:",
        "- Sentences 2 and 3 MUST refer to the SAME single topic — never two different topics.",
        "- The LAST sentence MUST be a question ending with ?",
        "- Max 45 words total. Skip preferences, drinks, birthdays, alarms.",
        "- Do NOT mention facial expression, mood, or how they look.",
        "- No quotes, bullet points, or stage directions.",
    ]
    if the_topic:
        lines += [
            "",
            f"Use ONLY this topic for BOTH sentence 2 and sentence 3: {the_topic}.",
            "Good examples (do not copy verbatim, keep the same single topic):",
            f'Hi {name}, good to see you! Yesterday we discussed {the_topic}. Want to pick up from there?',
            f'Hi {name}, good to see you! Yesterday we discussed {the_topic}. Can you tell me more about {the_topic}?',
        ]
    else:
        lines += [
            "",
            "Good examples (do not copy verbatim; pick ONE real topic from the summary "
            "and use it in BOTH sentence 2 and sentence 3):",
            f'Hi {name}, good to see you! Yesterday we discussed that topic. Want to pick up from there?',
        ]
    if summary:
        lines.append(f"Prior-day session summary:\n{summary}")
    else:
        lines.append("No prior-day summary — warm greeting only, offer help briefly.")
    return "\n".join(lines)


def _ends_with_invitation_question(text: str) -> bool:
    tail = text.strip()[-120:]
    return "?" in tail


def _invite_continuation_via_llm(
    greeting_so_far: str,
    display_name: str,
    primary_topic: str,
    summary_text: str,
    *,
    model: str | None = None,
    api_url: str | None = None,
    timeout_s: int = VOICE_QUERY_TIMEOUT_S,
) -> str:
    """Ask the LLM for sentence 3 when the fallback full-greeting path omitted it."""
    body = greeting_so_far.strip()
    topic = primary_topic.strip() or "our chat"
    y_marker = "yesterday we discussed"
    y_idx = body.lower().find(y_marker)
    if y_idx >= 0:
        hello = body[:y_idx].strip().rstrip(".!?")
        rest = body[y_idx:]
        dot = rest.find(".")
        yesterday = rest[: dot + 1].strip() if dot >= 0 else rest.strip()
    else:
        hello = body.rstrip(".!?") or build_startup_greeting_hello(display_name)
        yesterday = build_startup_greeting_yesterday(topic)
    return _startup_closing_question_via_llm(
        display_name,
        topic,
        hello,
        yesterday,
        summary_text,
        model=model,
        api_url=api_url,
        timeout_s=timeout_s,
    )


def startup_greeting_parts_from_summary(
    display_name: str,
    summary_text: str,
    *,
    model: str | None = None,
    api_url: str | None = None,
    timeout_s: int = VOICE_QUERY_TIMEOUT_S,
) -> StartupGreetingParts | None:
    """Build hello + yesterday (fixed) + LLM counter-question."""
    name = display_name.strip()
    summary = summary_text.strip()
    if not summary:
        return None
    topics = parse_summary_topics_for_greeting(summary)
    primary_topic = topics[0] if topics else ""
    if not primary_topic:
        return None

    hello = build_startup_greeting_hello(name)
    yesterday = build_startup_greeting_yesterday(primary_topic)
    closing = _startup_closing_question_via_llm(
        name,
        primary_topic,
        hello,
        yesterday,
        summary,
        model=model,
        api_url=api_url,
        timeout_s=timeout_s,
    )
    if not closing:
        closing = "Want to pick up from there?"
    return StartupGreetingParts(hello=hello, yesterday=yesterday, question=closing)


def finalize_startup_greeting(
    text: str,
    display_name: str,
    *,
    primary_topic: str = "",
    summary_text: str = "",
    model: str | None = None,
    api_url: str | None = None,
    timeout_s: int = VOICE_QUERY_TIMEOUT_S,
) -> str:
    """Ensure the spoken greeting ends with an LLM-framed closing question."""
    body = text.strip()
    if not body:
        return body
    if _ends_with_invitation_question(body):
        return body
    topic = primary_topic.strip() or "our chat"
    invite = _invite_continuation_via_llm(
        body,
        display_name,
        topic,
        summary_text,
        model=model,
        api_url=api_url,
        timeout_s=timeout_s,
    )
    if invite:
        return f"{body.rstrip('.! ')} {invite}"
    return f"{body.rstrip('.! ')} Want to pick up from there?"


def startup_greeting_from_summary(
    display_name: str,
    summary_text: str,
    *,
    model: str | None = None,
    api_url: str | None = None,
    num_predict: int = 80,
    timeout_s: int = VOICE_QUERY_TIMEOUT_S,
) -> str:
    """Startup greeting: fixed opener from summary topic + LLM closing question."""
    name = display_name.strip()
    summary = summary_text.strip()
    if not summary:
        return greeting_for_face(
            name,
            is_return_visitor=False,
            session_summary=None,
            is_startup_greeting=True,
            model=model,
            api_url=api_url,
            num_predict=num_predict,
            timeout_s=timeout_s,
        )

    topics = parse_summary_topics_for_greeting(summary)
    primary_topic = topics[0] if topics else ""
    parts = startup_greeting_parts_from_summary(
        name,
        summary,
        model=model,
        api_url=api_url,
        timeout_s=timeout_s,
    )
    if parts is not None:
        return parts.spoken()

    prompt = build_startup_summary_greeting_prompt(name, summary)
    top_p_raw = os.environ.get("STARTUP_GREETING_TOP_P")
    if top_p_raw is None:
        top_p_raw = os.environ.get("VOICE_REPLY_TOP_P", "0.92")

    text = ollama_generate(
        prompt,
        model=model,
        api_url=api_url,
        timeout_s=timeout_s,
        num_predict=num_predict,
        temperature=_startup_greeting_temperature(),
        top_p=float(top_p_raw),
    ).strip()
    if text:
        return finalize_startup_greeting(
            text,
            name,
            primary_topic=primary_topic,
            summary_text=summary,
            model=model,
            api_url=api_url,
            timeout_s=timeout_s,
        )

    return greeting_for_face(
        name,
        is_return_visitor=False,
        session_summary=summary,
        is_startup_greeting=True,
        model=model,
        api_url=api_url,
        num_predict=num_predict,
        timeout_s=timeout_s,
    )


def greeting_for_face(
    display_name: str,
    *,
    is_return_visitor: bool,
    session_summary: str | None = None,
    is_startup_greeting: bool = False,
    model: str | None = None,
    api_url: str | None = None,
    num_predict: int = 48,
    timeout_s: int = VOICE_QUERY_TIMEOUT_S,
) -> str:
    prompt = build_greeting_prompt(
        display_name,
        is_return_visitor=is_return_visitor,
        session_summary=session_summary,
        is_startup_greeting=is_startup_greeting,
    )
    if (session_summary or "").strip():
        num_predict = min(max(num_predict, 64), 80)
    return ollama_generate(
        prompt,
        model=model,
        api_url=api_url,
        timeout_s=timeout_s,
        num_predict=num_predict,
    )


def answer_identity_question(
    user_text: str,
    *,
    registered_name: str | None,
    recognition_state: str,
    model: str | None = None,
    api_url: str | None = None,
    max_words: int = 45,
    memory_context: str | None = None,
) -> str:
    """Answer 'who am I?' style questions using live camera recognition context."""
    if recognition_state == "recognized" and registered_name:
        camera_ctx = (
            f"The camera face recognition system has identified the person in front "
            f"of the camera as: {registered_name.strip()}."
        )
        rules = (
            "Answer the user's identity question using ONLY that recognized name. "
            "Do not guess, invent, or mention any other name."
        )
    elif recognition_state == "unknown":
        camera_ctx = (
            "The camera sees a face, but that person is NOT registered in the system "
            "(recognition state: unknown)."
        )
        rules = (
            "Tell them politely they are not registered yet and should register their "
            "face on the NiNO camera web page. Do not invent or guess a name."
        )
    else:
        camera_ctx = (
            "No face is clearly visible in front of the camera right now "
            "(recognition state: no face)."
        )
        rules = (
            "Explain you cannot identify them right now — ask them to step in front of "
            "the camera, or register on the NiNO camera web page if they have not yet. "
            "Do not invent or guess a name."
        )

    prompt = (
        "You are NiNO, a concise voice assistant for a smart home with a camera.\n"
        f"{camera_ctx}\n"
        f"{rules}\n"
        f"{_memory_context_block(memory_context)}"
        f"Rules: one short spoken reply under {max_words} words, plain sentences, "
        "no lists, no markdown, no emojis, no stage directions, suitable to read aloud. "
        "Use fresh casual wording; do not sound like a fixed template.\n"
        f"The user asked: {user_text}"
    )
    return ollama_generate(
        prompt,
        model=model,
        api_url=api_url,
        num_predict=96,
        timeout_s=VOICE_QUERY_TIMEOUT_S,
        temperature=_voice_reply_temperature(),
        top_p=_voice_reply_top_p(),
    )


def _memory_context_block(memory_context: str | None) -> str:
    block = (memory_context or "").strip()
    if not block:
        return ""
    return f"{block}\n\n"


_CONVERSATION_RECAP_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bwhat (?:did|have) we (?:just )?(?:talk(?:ed|ing)?|discuss(?:ed|ing)?)(?:\s+about)?\b",
        r"\bwhat we (?:just )?(?:talked|discussed)(?:\s+about)?\b",
        r"\bwhat were we (?:just )?talking about\b",
        r"\bwhat (?:are|were) we (?:discussing|talking about)\b",
        r"\bwhat(?:\s+are|'re)\s+we\s+discussing(?:\s+right\s+now|\s+now)?\b",
        r"\bwhat (?:are|were) you (?:discussing|talking about)\b",
        r"(?:please\s+)?(?:tell me|say)\s+what (?:you|we) (?:are|were) (?:discussing|talking about)\b",
        r"(?:repeat|recap|remind me).{0,24}what we (?:just )?(?:talked|discussed)\b",
        r"tell me what we (?:just )?(?:talked|discussed)\b",
        r"\bwhat(?:'s| is) our conversation about\b",
        r"\bwhat(?:'s| is) (?:our|the) (?:discussion|conversation) about\b",
        r"\bwhat we are discussing(?:\s+now)?\b",
        r"\bwhat did we (?:just )?talk(?:\s+about)?(?:\s+now)?\b",
        r"\b(?:so,?\s*)?(?:here\s+)?we just (?:discussed|talked)\b",
        r"\bwhat did i (?:just )?(?:ask|say|talk about)\b",
        r"\bwhat (?:have )?i (?:just )?asked(?:\s+earlier|\s+before)?\b",
        r"(?:please\s+)?(?:tell me|say) what i (?:just )?asked\b",
        r"\bwhat was my last question\b",
        r"\bwhat(?:'s| is) my last question\b",
        r"\bwhat was the last (?:question|thing) i (?:asked|said)\b",
        r"\bwhat(?:'s| is) the last (?:question|thing) i (?:asked|said)\b",
        r"\bremind me (?:what|of) (?:my|the) last question\b",
        r"\bwhat was my previous question\b",
        r"\brecap(?:ulate)? (?:our )?(?:chat|conversation|discussion)\b",
        r"\b(?:what(?:'s| is)\s+)?(?:the\s+)?context\b",
        r"\b(?:give|tell|share)\s+me\s+(?:the\s+)?context\b",
        r"\bcontext\s+so\s+far\b",
        r"\bwhatever we (?:are |'re )?(?:discussing|talking about)\b",
        r"(?:please\s+)?(?:describe|explain|summarize|summarise)(?:\s+me)?\s+(?:whatever|what)\s+we (?:are |'re )?(?:discussing|talking about)\b",
        r"(?:please\s+)?(?:describe|explain|summarize|summarise).{0,32}(?:discussing|talking about)(?:\s+right\s+now|\s+now|\s+today)?\b",
        r"\brecap\b.{0,48}\b(?:talking|discussing)\b",
        r"\bwhat we are (?:talking|discussing)\b",
        r"\bwe(?:'re| are) discussing(?:\s+right\s+now|\s+now)?\b",
        r"\bcontext of what we\b",
        r"\bin this view.{0,40}recap\b",
        r"^what we are discussing[.!?…]*\s*$",
        r"\bplease give me the context\b",
        r"\bwe(?:'re| are) (?:talking|discussing) about\b",
        r"\bso we(?:'re| are)? (?:talking|discussing) about\b",
        r"\baren't we (?:talking|discussing)(?:\s+about)?\b",
        r"\bare we (?:talking|discussing) about\b",
        r"\bhope.{0,24}(?:we(?:'re| are)|that we).{0,24}(?:talking|discussing) about\b",
        r"\b(?:isn't|is not) (?:that|this) what we(?:'re| are) (?:talking|discussing) about\b",
        # Past-time assumed discussion (must not fall through to general LLM)
        r"\b(?:few|several|couple of)\s+minutes?\s+(?:back|ago)\b.{0,48}(?:discuss|talk|chat|conversation)\b",
        r"\b(?:a while|some time)\s+ago\b.{0,40}(?:discuss|talk|chat|conversation)\b",
        r"\b(?:earlier|previously|before)(?:\s+today)?\s+we (?:had|were having)\s+(?:a\s+)?(?:discussion|conversation|chat)\b",
        r"\bwe (?:had|have had)\s+(?:a\s+)?(?:discussion|conversation|chat)\s+(?:on|about)\b",
        r"\bwe (?:just |already )?(?:discussed|talked about|chatted about)\b",
        r"\b(?:today|yesterday|this morning|this afternoon|last night)\b.{0,40}(?:discuss|talk|chat|conversation)\b",
        r"\bexplain (?:about )?that again\b",
        r"\b(?:please\s+)?(?:explain|tell me about|brief).{0,24}again\b",
    )
)

_RECAP_STYLE_VARIANTS: tuple[str, ...] = (
    "Use a warm conversational tone.",
    "Use a crisp concise tone.",
    "Use a slightly upbeat tone.",
    "Use a calm matter-of-fact tone.",
)

_VOICE_STYLE_VARIANTS: tuple[str, ...] = _RECAP_STYLE_VARIANTS + (
    "Use a friendly relaxed tone, like chatting at home.",
    "Use a lightly playful tone.",
    "Use a thoughtful helpful tone.",
    "Use an enthusiastic curious tone, like you are excited to keep talking.",
)


def _voice_reply_temperature() -> float:
    raw = os.environ.get("VOICE_REPLY_TEMPERATURE", "0.72").strip()
    try:
        return float(max(0.0, min(1.2, float(raw))))
    except ValueError:
        return 0.72


def _voice_reply_top_p() -> float:
    raw = os.environ.get("VOICE_REPLY_TOP_P", "0.92").strip()
    try:
        return float(max(0.05, min(1.0, float(raw))))
    except ValueError:
        return 0.92


# Chat replies used to open with hello / good evening on almost every turn.
# A greeting is now allowed roughly once every LLM_GREETING_ONE_IN chat turns.
DEFAULT_LLM_GREETING_ONE_IN = 20


def llm_greeting_one_in() -> int:
    raw = os.environ.get("LLM_GREETING_ONE_IN", str(DEFAULT_LLM_GREETING_ONE_IN)).strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_LLM_GREETING_ONE_IN


def greeting_allowed_for_llm_turn() -> bool:
    """True about once every llm_greeting_one_in() chat turns."""
    return random.randrange(llm_greeting_one_in()) == 0


_LEADING_GREETING_RE = re.compile(
    r"^\s*(?:hey|hi|hiya|hello|yo|good\s+(?:morning|afternoon|evening|day))"
    r"(?:\s+(?:there|again))?"
    r"(?:\s*,?\s*[a-z]+)?"
    r"\s*[,!.…-]+\s*",
    re.IGNORECASE,
)


def strip_leading_greeting(reply: str) -> str:
    """Drop a hello / good-evening opener so the answer starts on the content."""
    text = (reply or "").strip()
    for _ in range(2):
        stripped = _LEADING_GREETING_RE.sub("", text, count=1).strip()
        if not stripped or stripped == text:
            break
        text = stripped
    if not text:
        return reply
    if text[0].islower():
        text = text[0].upper() + text[1:]
    return text


def _anti_repetition_block(recent_assistant_replies: list[str] | None) -> str:
    if not recent_assistant_replies:
        return ""
    snippets: list[str] = []
    seen: set[str] = set()
    for text in reversed(recent_assistant_replies):
        cleaned = " ".join(str(text or "").strip().split())
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        snippets.append(cleaned)
        if len(snippets) >= 4:
            break
    if not snippets:
        return ""
    quoted = "; ".join(f'"{s[:120]}"' for s in reversed(snippets))
    return (
        "Do NOT reuse these exact prior replies — same facts, new wording and tone:\n"
        f"{quoted}\n"
    )


def _defers_to_recognized_speaker(reply: str, viewer_name: str | None) -> bool:
    """Detect a reply that mistakenly treats the current speaker as someone else."""
    name = (viewer_name or "").strip()
    if not name:
        return False

    escaped_name = re.escape(name)
    text = " ".join(reply.split())
    return bool(
        re.search(
            rf"\b(?:ask|check with|talk to|consult)\s+{escaped_name}\b",
            text,
            re.IGNORECASE,
        )
        or re.search(
            rf"\b{escaped_name}\s+(?:might|may|could|would|will)\s+know\b",
            text,
            re.IGNORECASE,
        )
    )


def is_conversation_recap_question(user_text: str) -> bool:
    text = user_text.strip()
    if not text:
        return False
    return any(p.search(text) for p in _CONVERSATION_RECAP_PATTERNS)


_LAST_QUESTION_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bwhat was my last question\b",
        r"\bwhat(?:'s| is) my last question\b",
        r"\bwhat was the last (?:question|thing) i (?:asked|said)\b",
        r"\bwhat(?:'s| is) the last (?:question|thing) i (?:asked|said)\b",
        r"\bremind me (?:what|of) (?:my|the) last question\b",
        r"\bwhat was my previous question\b",
        r"\bwhat did i (?:just )?(?:ask|say)\b",
        r"\bwhat (?:have )?i (?:just )?asked(?:\s+earlier|\s+before)?\b",
        r"(?:please\s+)?(?:tell me|say) what i (?:just )?asked\b",
    )
)


def is_last_question_query(user_text: str) -> bool:
    """True when the user wants the previous question they asked, not a full recap."""
    text = user_text.strip()
    if not text:
        return False
    return any(p.search(text) for p in _LAST_QUESTION_PATTERNS)


def last_user_question_from_history(
    recent_history: list[tuple[str, str]] | None,
) -> str | None:
    """Newest stored user line that is a real question, not a recap/last-question ask."""
    from memory_service import is_stt_fragment

    if not recent_history:
        return None
    for user_text, _assistant_text in reversed(recent_history):
        cleaned = str(user_text or "").strip()
        if not cleaned:
            continue
        if is_last_question_query(cleaned) or is_conversation_recap_question(cleaned):
            continue
        if is_stt_fragment(cleaned):
            continue
        return cleaned
    return None


def answer_last_user_question(
    last_question: str | None,
    *,
    viewer_name: str | None = None,
    has_face: bool = True,
) -> str:
    """Deterministic spoken reply — do not ask the LLM to guess the last question."""
    if not has_face:
        return (
            "I need to see you to recall your last question. "
            "Face the camera and ask again."
        )
    cleaned = " ".join((last_question or "").split()).rstrip(" ?.!")
    if not cleaned:
        return (
            "I don't have a previous question stored yet. "
            "Ask me something and I'll remember it."
        )
    name = (viewer_name or "").strip()
    spoken = cleaned[0].lower() + cleaned[1:] if len(cleaned) > 1 else cleaned.lower()
    if name:
        return f"{name}, you asked {spoken}."
    return f"You asked {spoken}."


_TOPIC_FOCUSED_RECAP_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bhope.{0,30}(?:talking|discussing) about\b",
        r"\baren't we (?:talking|discussing) about\b",
        r"\bare we (?:talking|discussing) about\b",
        r"\bso,?\s*we(?:'re| are)?\s+(?:talking|discussing) about\b",
        r"\bisn't that what we(?:'re| are) (?:talking|discussing) about\b",
        r"\b(?:today|yesterday|earlier|just now|this morning|this afternoon|last night)\b.{0,32}(?:talking|discussing|discuss|talk|chat)\b",
        r"\bwe(?:'re| are)\s+(?:talking|discussing) about\b",
        r"\b(?:few|several|couple of)\s+minutes?\s+(?:back|ago)\b",
        r"\b(?:a while|some time)\s+ago\b",
        r"\b(?:earlier|previously|before)(?:\s+today)?\s+we (?:had|were having|discussed|talked)\b",
        r"\bwe (?:had|have had)\s+(?:a\s+)?(?:discussion|conversation|chat)\s+(?:on|about)\b",
        r"\bwe (?:just |already )?(?:discussed|talked about|chatted about)\b",
    )
)

_TALKING_ABOUT_TOPIC = re.compile(
    r"(?:talking|discussing)\s+about\s+(?:something\s+(?:called\s+)?)?",
    re.IGNORECASE,
)

_TOPIC_FROM_DISCUSSION_ON = re.compile(
    r"(?:had|having)\s+(?:a\s+)?(?:discussion|conversation|chat)\s+(?:on|about)\s+(?:the\s+)?"
    r"(.+?)(?:\s+right|\s+correct)?(?:\s*[?.!,;]|\s+please\b|\s+could\b|\s+would\b|$)",
    re.IGNORECASE,
)

_TOPIC_FROM_DISCUSSED = re.compile(
    r"\b(?:discussed|talked about|chatted about)\s+(?:the\s+)?"
    r"(.+?)(?:\s+right|\s+correct)?(?:\s*[?.!,;]|\s+please\b|\s+could\b|\s+would\b|$)",
    re.IGNORECASE,
)

_TOPIC_EXTRACT = re.compile(
    r"(?:talking|discussing) about\s+(?:something\s+(?:called\s+)?)?"
    r"(.+?)(?:\s+right|\s+correct)?\s*\?",
    re.IGNORECASE,
)

_BRIEF_OR_EXPLAIN_REQUEST = re.compile(
    r"\b(?:brief(?:\s+out)?|explain|tell me about|describe|walk me through)\b",
    re.IGNORECASE,
)


def is_topic_focused_recap(user_text: str) -> bool:
    """User assumes a specific topic was already being discussed."""
    text = user_text.strip()
    if not text:
        return False
    if any(p.search(text) for p in _TOPIC_FOCUSED_RECAP_PATTERNS):
        return True
    return bool(_TALKING_ABOUT_TOPIC.search(text) and extract_recap_focus_topic(text))


def is_assumed_prior_topic_question(user_text: str) -> bool:
    """Same as topic-focused recap — alias for voice routing."""
    return is_topic_focused_recap(user_text)


def user_requests_topic_brief(user_text: str) -> bool:
    return bool(_BRIEF_OR_EXPLAIN_REQUEST.search(user_text.strip()))


_RECAP_BRIEF_ONLY = re.compile(
    r"\b(?:brief(?:\s+(?:me|out|that))?|recap(?:ulate)?|context|summarize|summarise|"
    r"remind me what we|what (?:did|have) we (?:talked|discussed)|what were we (?:talking|discussing))\b",
    re.IGNORECASE,
)


def is_substantive_recap_follow_up(follow_up: str) -> bool:
    """True when the tail after recap framing is a real question, not just 'brief me'."""
    q = follow_up.strip()
    if len(q) < 8:
        return False
    lower = q.lower()
    if _RECAP_BRIEF_ONLY.search(lower):
        return False
    if re.search(
        r"^(?:please\s+)?(?:could you\s+)?(?:explain|tell me about|describe|brief)\s+(?:about\s+)?that[.!?…]*$",
        lower,
    ):
        return False
    if re.search(r"\b(?:what|how|which|why|who|when|where)\b", lower):
        return True
    if re.search(r"\b(?:explain|describe|list|name|compare|add)\b", lower):
        return True
    return len(q) >= 24


def extract_recap_follow_up_question(user_text: str) -> str | None:
    """When recap framing is followed by a real question, return that question part."""
    text = user_text.strip()
    if not text or not is_conversation_recap_question(text):
        return None

    if "?" in text:
        after_first = text.split("?", 1)[1].strip().lstrip(",").strip()
        if is_substantive_recap_follow_up(after_first):
            return after_first

    for pattern in (
        r"\b(?:right|correct)\??\s*[,]?\s*(?:and\s+)?(?P<rest>.+)$",
        r"\b(?:right|correct)\s+(?P<rest>.+)$",
    ):
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        rest = match.group("rest").strip()
        sub = re.search(
            r"\b(?:and\s+)?((?:what|how|which|why|who|when|where)\s+.+)$",
            rest,
            re.IGNORECASE,
        )
        if sub and is_substantive_recap_follow_up(sub.group(1)):
            return sub.group(1).strip()
        if is_substantive_recap_follow_up(rest):
            return rest

    return None


def is_recap_with_follow_up_question(user_text: str) -> bool:
    return extract_recap_follow_up_question(user_text) is not None


def normalize_recap_focus_topic(raw: str) -> str:
    topic = raw.strip().rstrip(".!?…,")
    topic = re.sub(r"^(?:something\s+)?(?:called\s+)?", "", topic, flags=re.IGNORECASE)
    topic = re.sub(r"^(?:a|an|the)\s+", "", topic, flags=re.IGNORECASE)
    topic = re.split(r"[?.!,;]", topic, maxsplit=1)[0].strip()
    topic = re.sub(r"\s+(?:right|correct)$", "", topic, flags=re.IGNORECASE)
    return topic


_TOPIC_TAIL_BOUNDARY = re.compile(
    r"\s+right(?:\s|$)|"
    r"\s+correct(?:\s|$)|"
    r"\s*,\s*|"
    r"\s+(?:could|can|would)\s+you\b|"
    r"\s+please\b|"
    r"\s+and\s+(?:on top of that|also|what|how|could|please)\b|"
    r"\s+(?:brief|explain|tell me|describe|walk me through)\b",
    re.IGNORECASE,
)


def _trim_recap_topic_chunk(chunk: str) -> str:
    """Keep only the subject before 'right', 'could you brief', 'and what…', etc."""
    text = chunk.strip()
    match = _TOPIC_TAIL_BOUNDARY.search(text)
    if match:
        text = text[: match.start()].strip()
    return normalize_recap_focus_topic(text)


def extract_recap_focus_topic(user_text: str) -> str | None:
    """Pull the subject from recap phrasing (talking about X, discussion on X, etc.)."""
    text = user_text.strip()
    match = _TOPIC_EXTRACT.search(text)
    if match:
        topic = _trim_recap_topic_chunk(match.group(1))
        if len(topic) >= 3:
            return topic
    for pattern in (_TOPIC_FROM_DISCUSSION_ON, _TOPIC_FROM_DISCUSSED):
        match = pattern.search(text)
        if match:
            topic = _trim_recap_topic_chunk(match.group(1))
            if len(topic) >= 3:
                return topic
    fallback = _TALKING_ABOUT_TOPIC.search(text)
    if fallback:
        after = text[fallback.end() :]
        chunk = re.split(r"[?.!,;]", after, maxsplit=1)[0]
        topic = _trim_recap_topic_chunk(chunk)
        if len(topic) >= 3:
            return topic
    return None


def recap_turn_matches_topic(
    topic: str, user_text: str, assistant_text: str
) -> bool:
    topic_clean = normalize_recap_focus_topic(topic).lower()
    if not topic_clean:
        return False
    blob = f"{user_text} {assistant_text}".lower()
    if topic_clean in blob:
        return True
    tokens = [w for w in re.findall(r"[a-z0-9]+", topic_clean) if len(w) >= 4]
    if len(tokens) >= 2:
        return all(token in blob for token in tokens)
    if tokens:
        return tokens[0] in blob
    short_tokens = [w for w in re.findall(r"[a-z0-9]+", topic_clean) if len(w) >= 3]
    return bool(short_tokens) and all(t in blob for t in short_tokens)


def recap_topic_not_found_reply(
    topic: str,
    *,
    person_name: str = "",
) -> str:
    """Deterministic reply — never hallucinate a briefing without DB context."""
    clean = normalize_recap_focus_topic(topic) or topic.strip()
    prefix = f"{person_name}, " if person_name else ""
    return (
        f"{prefix}I don't have {clean} in our conversation history yet. "
        "Shall we discuss it now?"
    )


def answer_recap_contextual_question(
    user_text: str,
    follow_up_question: str,
    *,
    viewer_name: str | None,
    memory_context: str,
    focus_topic: str | None = None,
    model: str | None = None,
    api_url: str | None = None,
    max_words: int = 60,
) -> str:
    """Answer a new question grounded in recalled session history (not recap-only)."""
    name = (viewer_name or "").strip()
    history = memory_context.strip()
    topic_line = (
        f"The user assumes you were already discussing '{focus_topic}'.\n"
        if focus_topic
        else ""
    )
    who = (
        f"Speaking to {name}. Use second person (you/we). Name at most once.\n"
        if name
        else "Use second person (you/we).\n"
    )
    prompt = (
        "You are NiNO, a concise voice assistant.\n"
        f"{who}"
        f"{topic_line}"
        "Session history about this topic (newest turns first):\n"
        f"{history}\n\n"
        "The user's full message:\n"
        f"{user_text.strip()}\n\n"
        "Their specific question you must answer now:\n"
        f"{follow_up_question.strip()}\n\n"
        "Rules:\n"
        "- Answer the specific question directly — do not only summarize prior chat.\n"
        "- Ground in session history when it helps; you may add brief factual detail to answer well.\n"
        "- Do not invent things that were never said in the history.\n"
        "- If they confirm the topic ('we are talking about X right?'), acknowledge briefly then answer.\n"
        f"- One spoken reply under {max_words} words. Plain sentences, no lists, markdown, or emojis.\n"
    )
    return ollama_generate(
        prompt,
        model=model,
        api_url=api_url,
        num_predict=128,
        timeout_s=VOICE_QUERY_TIMEOUT_S,
        temperature=0.55,
        top_p=0.9,
    )


def answer_conversation_recap(
    user_text: str,
    *,
    viewer_name: str | None,
    recognition_state: str,
    memory_context: str | None = None,
    focus_topic: str | None = None,
    model: str | None = None,
    api_url: str | None = None,
    max_words: int = 45,
) -> str:
    name = (viewer_name or "").strip()
    history = (memory_context or "").strip()
    if focus_topic and not history:
        return recap_topic_not_found_reply(focus_topic, person_name=name)
    style_hint = random.choice(_RECAP_STYLE_VARIANTS)

    if recognition_state == "recognized" and name:
        identity_rules = (
            f"The live camera currently recognizes the user as {name}.\n"
            "You may use their name at most once, naturally.\n"
            f"Use second person only (you/we). Never say \"{name} and ...\" or refer to them in third person.\n"
        )
    elif recognition_state == "unknown":
        identity_rules = (
            "A face is visible, but the user is not recognized in the face database.\n"
            "Say you cannot pull personal context yet and ask them to register or re-center for recognition.\n"
            "Do not guess or invent any name.\n"
        )
    else:
        identity_rules = (
            "No recognized face is currently available for this context request.\n"
            "Politely say you cannot access personal context right now and ask them to face the camera.\n"
            "Do not guess or invent any name.\n"
        )

    history_rules = (
        "Session history is provided below. Summarize the latest discussion across all provided turns (up to 5).\n"
        "If the user is checking whether you are discussing a specific topic (e.g. 'aren't we talking about X?'), "
        "answer yes or no based ONLY on the session history, then briefly say what you were discussing.\n"
        "Ignore incomplete speech-to-text fragments.\n"
        if history and not focus_topic
        else (
            f"The user is asking specifically about '{focus_topic}'.\n"
            "Use ONLY the session turns below that mention this topic.\n"
            "Do NOT mention birthdays, food, drinks, sports, or any unrelated subject.\n"
            "If the turns support it, confirm briefly and explain only what was said about this topic.\n"
            "Never bring in other topics from earlier chats.\n"
            if history and focus_topic
            else (
                "No usable conversation history is available for this user yet.\n"
                "Say that briefly and ask one short follow-up prompt to continue.\n"
            )
        )
    )

    style_rules = (
        f"- {style_hint}\n"
        "- Give a compact natural summary in 1-2 short sentences.\n"
        "- Stay on the asked topic only; do not drift to unrelated stored facts.\n"
        if focus_topic
        else (
            f"- {style_hint}\n"
            "- Give a compact natural summary in 2-3 short sentences.\n"
            "- Cover the key points from the provided recent turns (aim for 3-5 points when available), not just one topic.\n"
        )
    )

    prompt = (
        "You are NiNO, a concise voice assistant.\n"
        f"{identity_rules}"
        "The user asked for context/recap.\n"
        f"{history_rules}"
        f"{_memory_context_block(history)}"
        "Style rules for recap quality:\n"
        f"{style_rules}"
        "- Use fresh wording each time; avoid repeating the same sentence structure on every recap.\n"
        "- Do NOT start with or repeat 'You asked about...'.\n"
        "- Never use bullet points, numbering, or list formatting.\n"
        "- Avoid echoing exact lines from history unless necessary.\n"
        f"Rules: one concise spoken reply under {max_words} words, plain sentences, "
        "no lists, no markdown, no emojis, no stage directions, suitable to read aloud.\n"
        f"The user asked: {user_text}"
    )
    reply = ollama_generate(
        prompt,
        model=model,
        api_url=api_url,
        num_predict=96,
        timeout_s=VOICE_QUERY_TIMEOUT_S,
        temperature=0.7,
        top_p=0.92,
    )
    cleaned = " ".join(reply.strip().split())
    lower = cleaned.lower()
    words = [w for w in cleaned.split(" ") if w]
    needs_rewrite = (
        lower.startswith("you asked about")
        or lower.startswith("you just asked")
        or "\n" in reply
        or "- " in reply
        or "*" in reply
        or len(words) > max_words
    )
    if needs_rewrite:
        rewrite_prompt = (
            "Rewrite this context recap for voice playback.\n"
            f"Strict rules: 2-3 short sentences, max {max_words} words, no lists or bullets, "
            "no markdown, no emojis, and natural spoken style.\n"
            "Cover key points from recent turns (aim for 3-5 when available), not just one point.\n"
            "Use different wording from previous recap responses.\n"
            "Do not begin with 'You asked about'.\n\n"
            f"Original recap:\n{cleaned}"
        )
        rewritten = ollama_generate(
            rewrite_prompt,
            model=model,
            api_url=api_url,
            num_predict=96,
            timeout_s=VOICE_QUERY_TIMEOUT_S,
            temperature=0.75,
            top_p=0.95,
        )
        rewritten_clean = " ".join(rewritten.strip().split())
        trimmed = rewritten_clean.split(" ")
        if len(trimmed) > max_words:
            rewritten_clean = " ".join(trimmed[:max_words]).rstrip(",;:-") + "."
        return rewritten_clean
    return cleaned


def answer_voice_query(
    user_text: str,
    *,
    viewer_name: str | None = None,
    model: str | None = None,
    api_url: str | None = None,
    max_words: int = 40,
    memory_context: str | None = None,
    recent_assistant_replies: list[str] | None = None,
    is_follow_up: bool = False,
    vision_context: str | None = None,
) -> str:
    if viewer_name:
        who = (
            f"You are speaking directly to {viewer_name.strip()}, identified by the home camera. "
            "Use second person (you/we). Use their name at most once, naturally. "
            "Never refer to them by name in third person."
        )
    else:
        who = (
            "No registered person is clearly in front of the camera right now. "
            "Answer the question directly; do not invent or guess a name."
        )

    style_hint = random.choice(_VOICE_STYLE_VARIANTS)
    from alarm_time import day_part, day_part_greeting, system_now

    now = system_now()
    part = day_part(now)
    tod_greeting = day_part_greeting(now)
    allow_greeting = greeting_allowed_for_llm_turn()
    if allow_greeting:
        greeting_line = (
            f"If they greeted you, say \"{tod_greeting}\" — never the wrong period. "
        )
    else:
        greeting_line = (
            "Do NOT greet at all: no hi, hello, hey, good morning, good afternoon, "
            "or good evening. Start straight with the answer or your reaction. "
        )
    clock_line = (
        f"Current local time is {now.strftime('%I:%M %p').lstrip('0')} ({part}). "
        "Use this clock only if they asked the time. "
        f"{greeting_line}"
        "If they asked a factual question, do NOT greet; answer the question first.\n"
    )
    vision_rules = ""
    scene = (vision_context or "").strip()
    if scene:
        vision_rules = (
            f"Your camera can see right now: {scene}. "
            "Use this only when they ask what you can see or about objects around them. "
            "Describe it naturally in a sentence; never read it out as a list of labels, "
            "and never claim to see anything that is not listed.\n"
        )
    memory_rules = ""
    if memory_context:
        name_hint = viewer_name.strip() if viewer_name else "them"
        memory_rules = (
            "Known facts and recent conversation are below. Use ONLY facts relevant to "
            "the user's current question. "
            "Speak directly to them in second person. "
            f'Never say "You and {name_hint}" or use their name in third person. '
            "Do not mention birthdays, hobbies, or jokes unless the user asked about them. "
            "Ignore incomplete fragment lines.\n"
            f"{_anti_repetition_block(recent_assistant_replies)}"
        )
    follow_up_rules = ""
    if is_follow_up:
        follow_up_rules = (
            "- This is a follow-up. Continue the MOST RECENT topic in the conversation, "
            "not an older one and not both at once.\n"
            "- If the user gives numbers or a math expression, compute the answer immediately — "
            "do not ask for more numbers.\n"
            "- Give a real enthusiastic answer with a new interesting detail. "
            "Do not ask what they want to know first, and do not restart with hi/hello.\n"
            "- If they said a short topic like \"the Mars\", treat it as continue that topic.\n"
        )

    prompt = (
        "You are NiNO, an enthusiastic voice assistant for a smart home with a camera.\n"
        f"{who}\n"
        f"{clock_line}"
        f"{vision_rules}"
        f"{memory_rules}"
        f"{_memory_context_block(memory_context)}"
        "Style rules:\n"
        f"- {style_hint}\n"
        "- Sound warm, curious, and interactive — like a lively friend, not a helpdesk.\n"
        "- Never start a factual answer with good morning/afternoon/evening or their name as a greeting.\n"
        "- Never say \"how can I assist you\", \"how may I help\", "
        "\"further assistance\", or similar service-desk phrases.\n"
        "- Do not ask about their day unless they brought it up.\n"
        f"{follow_up_rules}"
        "- If the user shares how they feel (e.g. \"I am great\"), acknowledge warmly and invite "
        "them to keep chatting — do not restart with a greeting or offer of assistance.\n"
        "- If the topic came up before, vary wording and tone — do not repeat the same sentence.\n"
        "- Answer ONLY what the user asked; do not bring up unrelated stored facts.\n"
        "- Answer general-knowledge questions yourself; do not tell the speaker to ask another person.\n"
        "- The recognized speaker is the person you are addressing, never a third party to ask about the answer.\n"
        "- If the user is saying goodbye / bye / ending the chat, reply with a short farewell only — "
        "do not ask a follow-up question or invite them to keep talking.\n"
        f"Rules: one short spoken reply under {max_words} words, plain sentences, "
        "no lists, no markdown, no emojis, no stage directions, suitable to read aloud.\n"
        f"The user said: {user_text}"
    )
    reply = ollama_generate(
        prompt,
        model=model,
        api_url=api_url,
        num_predict=96,
        timeout_s=VOICE_QUERY_TIMEOUT_S,
        temperature=_voice_reply_temperature(),
        top_p=_voice_reply_top_p(),
    )
    def finalize(text: str) -> str:
        return text if allow_greeting else strip_leading_greeting(text)

    if not _defers_to_recognized_speaker(reply, viewer_name):
        return finalize(reply)

    logger.warning(
        "Discarding voice reply that defers to recognized speaker | viewer=%s question=%s",
        viewer_name,
        user_text[:120],
    )
    retry_prompt = (
        "Rewrite the assistant reply below for a voice assistant.\n"
        "Answer the user's question directly using your own knowledge. Do not tell the "
        "user to ask, check with, or consult any person. The named viewer is the person "
        "speaking to you, not a third party.\n"
        f"Question: {user_text}\n"
        f"Invalid reply: {reply}\n"
        f"Return one plain spoken answer under {max_words} words."
    )
    retried_reply = ollama_generate(
        retry_prompt,
        model=model,
        api_url=api_url,
        num_predict=96,
        timeout_s=VOICE_QUERY_TIMEOUT_S,
        temperature=_voice_reply_temperature(),
        top_p=_voice_reply_top_p(),
    )
    if not _defers_to_recognized_speaker(retried_reply, viewer_name):
        return finalize(retried_reply)
    return "I am sorry, I cannot answer that reliably right now."


# ---------------------------------------------------------------------------
# LLM-driven memory: recall vs store vs chat
# ---------------------------------------------------------------------------


@dataclass
class MemoryTurnDecision:
    action: str  # chat | recall | store
    recall_keys: list[str] = field(default_factory=list)
    store: list[dict[str, Any]] = field(default_factory=list)


_MEMORY_SLOT_CATALOG = """
birthdate, anniversary, nickname,
favorite_food, favorite_drink, favorite_sport, favorite_color, favorite_movie,
favorite_music, favorite_game, favorite_book, favorite_show,
hobbies, job_title, employer, education, skills,
allergies, dietary_restrictions, health_conditions,
location, hometown, nationality, languages,
family, spouse, children, pets,
relationship_status, religion, goals, aspirations,
dislikes, preferences
"""


def _parse_memory_turn_json(raw: str) -> MemoryTurnDecision:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return MemoryTurnDecision(action="chat")
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return MemoryTurnDecision(action="chat")
    if not isinstance(data, dict):
        return MemoryTurnDecision(action="chat")
    action = str(data.get("action", "chat")).strip().lower()
    if action not in {"chat", "recall", "store"}:
        action = "chat"
    recall_keys = [
        str(k).strip()
        for k in (data.get("recall_keys") or [])
        if str(k).strip()
    ]
    store_raw = data.get("store") or data.get("memories") or []
    store: list[dict[str, Any]] = []
    if isinstance(store_raw, list):
        store = [item for item in store_raw if isinstance(item, dict)]
    return MemoryTurnDecision(action=action, recall_keys=recall_keys, store=store)


def analyze_memory_turn(
    user_text: str,
    *,
    person_name: str | None = None,
    known_memory_keys: list[str] | None = None,
    model: str | None = None,
    api_url: str | None = None,
) -> MemoryTurnDecision:
    """LLM decides whether the user is recalling, sharing, or just chatting."""
    who = (
        f"The speaker is {person_name.strip()}."
        if person_name and person_name.strip()
        else "The speaker is an identified user."
    )
    keys_hint = ""
    if known_memory_keys:
        keys_hint = (
            "Memory keys already stored for this user: "
            + ", ".join(known_memory_keys[:20])
            + ".\n"
        )
    prompt = (
        "You classify voice input for a personal memory system.\n"
        f"{who}\n"
        f"{keys_hint}"
        "Decide ONE action:\n"
        "- recall: user asks to retrieve **personal** saved facts about themselves "
        "(birthday, favorites, likes/dislikes, job, family, pets, allergies, etc.)\n"
        "- store: user explicitly shares durable personal facts worth remembering "
        "(preferences, dates, relationships, location, health, goals, etc.)\n"
        "- chat: general Q&A, jokes, trivia, commands, greetings, fragments, "
        "conversation topic checks ('are we talking about X?'), recap/context requests, "
        "or nothing durable to save\n\n"
        "NOT recall: 'are we discussing CEOs', 'what are we talking about', "
        "'hope we are talking about X' — those are conversation context, action=chat.\n"
        "STORE rules: only facts the USER stated; skip jokes, questions, "
        "transient mood, weather, things only an assistant would say.\n"
        "Preference corrections count as store (e.g. 'favorite food is biryani not lemon rice').\n"
        "CRITICAL: 'my favorite food is lemon rice', 'I love hiking', 'I hate mushrooms' "
        "are STORE (user is telling you), never recall.\n"
        "RECALL only when the user ASKS a question (what is my..., do you know my...).\n"
        "For each store item, memory must be a complete short fact sentence grounded in "
        "what the user said (not just one word).\n"
        "RECALL rules: pick snake_case keys to look up. Use catalog keys when possible.\n"
        f"Key catalog:{_MEMORY_SLOT_CATALOG}\n"
        "Return JSON only:\n"
        '{"action":"chat|recall|store","recall_keys":[],"store":'
        '[{"key":"favorite_food","memory":"Favorite food is biryani, not lemon rice","importance":8}]}\n\n'
        f"User said:\n{user_text.strip()}\n"
    )
    raw = ollama_generate(
        prompt,
        model=model,
        api_url=api_url,
        num_predict=200,
        timeout_s=VOICE_QUERY_TIMEOUT_S,
        temperature=0.1,
    )
    return _parse_memory_turn_json(raw)


def answer_memory_store_ack(
    user_text: str,
    stored_facts: list[str],
    *,
    person_name: str | None = None,
    model: str | None = None,
    api_url: str | None = None,
    max_words: int = 28,
) -> str:
    """Short spoken acknowledgment after the LLM chose to store facts."""
    facts = "; ".join(stored_facts[:3])
    who = f"Address them as {person_name.strip()}. " if person_name else ""
    prompt = (
        "You are NiNO, a friendly voice assistant.\n"
        f"{who}"
        f"The user said: {user_text.strip()}\n"
        f"You saved these personal facts: {facts}\n"
        f"Give ONE short spoken acknowledgment under {max_words} words. "
        "Confirm what you will remember. No lists, no markdown, no emojis.\n"
    )
    return ollama_generate(
        prompt,
        model=model,
        api_url=api_url,
        num_predict=64,
        timeout_s=VOICE_QUERY_TIMEOUT_S,
        temperature=0.5,
    )


def answer_memory_recall_reply(
    user_text: str,
    recalled_facts: list[str],
    *,
    person_name: str | None = None,
    model: str | None = None,
    api_url: str | None = None,
    max_words: int = 35,
) -> str:
    """Speak a recall answer from DB facts — always phrased by the LLM."""
    who = f"Speaking to {person_name.strip()}. " if person_name else ""
    if recalled_facts:
        facts = "; ".join(recalled_facts)
        empty_hint = ""
    else:
        facts = "(none — nothing saved for this yet)"
        empty_hint = (
            "No matching facts exist. Tell them naturally you have not learned that "
            "about them yet. Do not invent or guess.\n"
        )
    prompt = (
        "You are NiNO, a friendly voice assistant.\n"
        f"{who}"
        f"The user asked: {user_text.strip()}\n"
        f"Known facts from memory database: {facts}\n"
        f"{empty_hint}"
        f"Answer using ONLY those facts when they exist. Under {max_words} words. "
        "Second person. No markdown.\n"
    )
    return ollama_generate(
        prompt,
        model=model,
        api_url=api_url,
        num_predict=80,
        timeout_s=VOICE_QUERY_TIMEOUT_S,
        temperature=0.35,
    )
