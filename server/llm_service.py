"""Local LLM via Ollama HTTP API (same pattern as voice_optimized assistant)."""

from __future__ import annotations

import logging
import os
import random
import re
import shutil
import subprocess
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
    if explicit and explicit.lower() not in {"auto", "detect"}:
        return explicit

    m = (model or os.environ.get("OLLAMA_MODEL") or DEFAULT_MODEL).strip()
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


def greeting_for_face(
    display_name: str,
    *,
    is_return_visitor: bool,
    model: str | None = None,
    api_url: str | None = None,
    num_predict: int = 48,
    timeout_s: int = VOICE_QUERY_TIMEOUT_S,
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
        "no lists, no markdown, no stage directions, suitable to read aloud. "
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
        r"\brecap(?:ulate)? (?:our )?(?:chat|conversation|discussion)\b",
        r"\b(?:what(?:'s| is)\s+)?(?:the\s+)?context\b",
        r"\b(?:give|tell|share)\s+me\s+(?:the\s+)?context\b",
        r"\bcontext\s+so\s+far\b",
        r"\bwhatever we (?:are |'re )?(?:discussing|talking about)\b",
        r"(?:please\s+)?(?:describe|explain|summarize|summarise)(?:\s+me)?\s+(?:whatever|what)\s+we (?:are |'re )?(?:discussing|talking about)\b",
        r"(?:please\s+)?(?:describe|explain|summarize|summarise).{0,32}(?:discussing|talking about)(?:\s+right\s+now|\s+now|\s+today)?\b",
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


def is_conversation_recap_question(user_text: str) -> bool:
    text = user_text.strip()
    if not text:
        return False
    return any(p.search(text) for p in _CONVERSATION_RECAP_PATTERNS)


def answer_conversation_recap(
    user_text: str,
    *,
    viewer_name: str | None,
    recognition_state: str,
    memory_context: str | None = None,
    model: str | None = None,
    api_url: str | None = None,
    max_words: int = 45,
) -> str:
    name = (viewer_name or "").strip()
    history = (memory_context or "").strip()
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
        "Ignore incomplete speech-to-text fragments.\n"
        if history
        else (
            "No usable conversation history is available for this user yet.\n"
            "Say that briefly and ask one short follow-up prompt to continue.\n"
        )
    )

    prompt = (
        "You are NiNO, a concise voice assistant.\n"
        f"{identity_rules}"
        "The user asked for context/recap.\n"
        f"{history_rules}"
        f"{_memory_context_block(history)}"
        "Style rules for recap quality:\n"
        f"- {style_hint}\n"
        "- Give a compact natural summary in 2-3 short sentences.\n"
        "- Cover the key points from the provided recent turns (aim for 3-5 points when available), not just one topic.\n"
        "- Use fresh wording each time; avoid repeating the same sentence structure on every recap.\n"
        "- Do NOT start with or repeat 'You asked about...'.\n"
        "- Never use bullet points, numbering, or list formatting.\n"
        "- Avoid echoing exact lines from history unless necessary.\n"
        f"Rules: one concise spoken reply under {max_words} words, plain sentences, "
        "no lists, no markdown, no stage directions, suitable to read aloud.\n"
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
            "no markdown, and natural spoken style.\n"
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
    memory_rules = ""
    if memory_context:
        name_hint = viewer_name.strip() if viewer_name else "them"
        memory_rules = (
            "Known facts and recent user lines are below. Use them when relevant. "
            "Speak directly to them in second person. "
            f'Never say "You and {name_hint}" or use their name in third person. '
            "Ignore incomplete fragment lines.\n"
            f"{_anti_repetition_block(recent_assistant_replies)}"
        )

    prompt = (
        "You are NiNO, a concise voice assistant for a smart home with a camera.\n"
        f"{who}\n"
        f"{memory_rules}"
        f"{_memory_context_block(memory_context)}"
        "Style rules:\n"
        f"- {style_hint}\n"
        "- Sound natural and casual, not scripted or robotic.\n"
        "- If the topic came up before, vary wording and tone — do not repeat the same sentence.\n"
        f"Rules: one short spoken reply under {max_words} words, plain sentences, "
        "no lists, no markdown, no stage directions, suitable to read aloud.\n"
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
