"""Local LLM via Ollama HTTP API (same pattern as voice_optimized assistant)."""

from __future__ import annotations

import logging
import os
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
) -> str:
    url = (api_url or os.environ.get("OLLAMA_URL") or DEFAULT_OLLAMA_URL).strip()
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
            "Use their name once in a brief, natural way (start or end), then answer "
            "what they asked. Do not repeat the name more than once."
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
        num_predict=96,
        timeout_s=VOICE_QUERY_TIMEOUT_S,
    )
