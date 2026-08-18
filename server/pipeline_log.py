"""Verbose per-stage voice/cloud logs with stage latency and running total.

Grep: ``NINO |``
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger("nino.pipeline")

_tls = threading.local()
_NOISY_ACCESS_PATHS = (
    "/api/status",
    "/snapshot.jpg",
    "/video_feed",
    "/api/objects",
)

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RED = "\033[31m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_BLUE = "\033[34m"
_MAGENTA = "\033[35m"
_CYAN = "\033[36m"
_WHITE = "\033[37m"
_BRIGHT_RED = "\033[91m"
_BRIGHT_GREEN = "\033[92m"
_BRIGHT_YELLOW = "\033[93m"
_BRIGHT_BLUE = "\033[94m"
_BRIGHT_MAGENTA = "\033[95m"
_BRIGHT_CYAN = "\033[96m"
_BRIGHT_WHITE = "\033[97m"

_KIND_COLORS = {
    "ASR": _BRIGHT_MAGENTA,
    "LLM": _BRIGHT_BLUE,
    "TTS": _BRIGHT_GREEN,
    "DEVICE": _BRIGHT_CYAN,
    "CAMERA": _BRIGHT_YELLOW,
    "CLOUD": _WHITE,
    "IDENT": _MAGENTA,
    "TOTAL": _BOLD + _BRIGHT_WHITE,
}

_EVENT_COLORS = {
    "DONE": _BRIGHT_GREEN,
    "UP": _BRIGHT_GREEN,
    "ONLINE": _BRIGHT_GREEN,
    "CONNECT": _BRIGHT_GREEN,
    "QUERY": _BOLD + _BRIGHT_WHITE,
    "SERVER": _BOLD + _BRIGHT_WHITE,
    "START": _DIM + _CYAN,
    "SEND": _CYAN,
    "AUDIO": _DIM + _WHITE,
    "HTTP": _WHITE,
    "FAIL": _BRIGHT_RED,
    "DOWN": _BRIGHT_RED,
    "OFFLINE": _BRIGHT_RED,
    "DISCONNECT": _DIM + _RED,
}

_DETAIL_WIDTH = 48
_DEVICE_WIDTH = 14
_KIND_WIDTH = 6
_EVENT_WIDTH = 10


def begin_pipeline(
    *,
    device_id: str = "",
    turn: object = None,
    session: str = "",
    t0: float | None = None,
) -> None:
    """Bind this worker thread to a voice query so ASR/LLM/TTS can log totals."""
    _tls.device_id = str(device_id or "-")
    _tls.turn = turn
    _tls.session = str(session or "")
    _tls.t0 = float(t0 if t0 is not None else time.perf_counter())


def end_pipeline() -> None:
    for key in ("device_id", "turn", "session", "t0"):
        if hasattr(_tls, key):
            delattr(_tls, key)


def _ctx() -> tuple[str, object, str, float | None]:
    return (
        str(getattr(_tls, "device_id", "") or "-"),
        getattr(_tls, "turn", None),
        str(getattr(_tls, "session", "") or ""),
        getattr(_tls, "t0", None),
    )


def _seconds(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{max(0.0, float(value)):.3f}s"


def _clip(value: object, limit: int = 240) -> str:
    text = str(value or "").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _host(url: str) -> str:
    try:
        parsed = urlparse(str(url or ""))
        return parsed.netloc or parsed.path or str(url)
    except Exception:
        return str(url or "")


def _ellipsis(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[: width - 1] + "…"


def colors_enabled(use_colors: bool | None = None) -> bool:
    """Color pipeline lines unless tests or ``NINO_NO_COLOR=1`` turn them off.

    Cursor's agent/terminal environment sets ``NO_COLOR``, so that flag is ignored.
    """
    if use_colors is False:
        return False
    if os.environ.get("NINO_NO_COLOR", ""):
        return False
    return True


def _paint(text: str, *codes: str, enabled: bool) -> str:
    if not enabled or not codes or not text:
        return text
    return "".join(codes) + text + _RESET


def _kind_color(kind: str) -> str:
    return _KIND_COLORS.get(str(kind or "").upper(), _WHITE)


def _event_color(event: str) -> str:
    return _EVENT_COLORS.get(str(event or "").upper(), _WHITE)


def _latency_color(seconds: float | None) -> str:
    if seconds is None:
        return _DIM
    if seconds >= 1.0:
        return _BRIGHT_RED
    if seconds >= 0.40:
        return _BRIGHT_YELLOW
    return _DIM


def _status_color(status: object) -> str:
    text = str(status or "").strip()
    if text in {"error", "fail", "failed"}:
        return _BRIGHT_RED
    try:
        code = int(text)
    except (TypeError, ValueError):
        return ""
    if 200 <= code < 300:
        return _BRIGHT_GREEN
    if 400 <= code:
        return _BRIGHT_RED
    if 300 <= code < 400:
        return _BRIGHT_YELLOW
    return ""


def _color_detail(detail: str, *, status: object = None, enabled: bool) -> str:
    if not enabled or not detail or detail == "-":
        return _paint(detail, _DIM, enabled=enabled)
    status_color = _status_color(status)
    pieces: list[str] = []
    for part in detail.split(" "):
        if "=" not in part:
            pieces.append(part)
            continue
        key, value = part.split("=", 1)
        key_s = _paint(key, _DIM, enabled=True)
        eq = _paint("=", _DIM, enabled=True)
        if key == "status" and status_color:
            value_s = _paint(value, status_color, enabled=True)
        elif key in {"text", "prompt", "reply", "heard"}:
            value_s = _paint(value, _BRIGHT_WHITE, enabled=True)
        else:
            value_s = _paint(value, _WHITE, enabled=True)
        pieces.append(f"{key_s}{eq}{value_s}")
    return " ".join(pieces)


def format_pipeline_line(
    *,
    created: float | None = None,
    device: str = "-",
    turn: object = None,
    kind: str = "",
    event: str = "",
    detail: str = "-",
    stage: str = "-",
    total: str = "-",
    stage_s: float | None = None,
    total_s: float | None = None,
    status: object = None,
    use_colors: bool = False,
) -> str:
    """Aligned terminal line. Colors are optional; grep still uses ``NINO |``."""
    stamp = time.strftime("%H:%M:%S", time.localtime(created or time.time()))
    turn_txt = "-" if turn in (None, "", "-") else f"t{turn}"
    kind_txt = str(kind or "-").upper()
    event_txt = str(event or "-").upper()
    detail_txt = detail or "-"
    pad = max(0, _DETAIL_WIDTH - len(detail_txt))

    prefix = _paint("NINO", _DIM, _BOLD, enabled=use_colors)
    clock = _paint(stamp, _DIM, enabled=use_colors)
    device_s = _paint(
        _ellipsis(str(device or "-"), _DEVICE_WIDTH).ljust(_DEVICE_WIDTH),
        _BRIGHT_CYAN,
        enabled=use_colors,
    )
    turn_s = _paint(f"{turn_txt:>4}", _DIM, _YELLOW, enabled=use_colors)
    kind_s = _paint(
        f"{kind_txt:<{_KIND_WIDTH}}",
        _kind_color(kind_txt),
        enabled=use_colors,
    )
    event_s = _paint(
        f"{event_txt:<{_EVENT_WIDTH}}",
        _event_color(event_txt),
        enabled=use_colors,
    )
    detail_s = _color_detail(detail_txt, status=status, enabled=use_colors) + (" " * pad)
    stage_s_txt = _paint(
        f"{stage:>8}",
        _latency_color(stage_s),
        enabled=use_colors,
    )
    total_s_txt = _paint(
        f"{total:>8}",
        _BOLD,
        _latency_color(total_s),
        enabled=use_colors,
    )
    return (
        f"{prefix} | {clock}  {device_s} {turn_s}  {kind_s} {event_s} "
        f"{detail_s}  {stage_s_txt}  {total_s_txt}"
    )


class PipelineFormatter(logging.Formatter):
    """Column-aligned NINO lines, colorized when the terminal can show them."""

    def __init__(self, fmt: str | None = None, datefmt: str | None = None, style: str = "%", use_colors: bool | None = None) -> None:
        super().__init__(fmt=fmt, datefmt=datefmt, style=style)
        self.use_colors = use_colors

    def format(self, record: logging.LogRecord) -> str:
        kind = getattr(record, "nino_kind", None)
        if not kind:
            return record.getMessage()
        return format_pipeline_line(
            created=record.created,
            device=str(getattr(record, "nino_device", "-")),
            turn=getattr(record, "nino_turn", None),
            kind=str(kind),
            event=str(getattr(record, "nino_event", "")),
            detail=str(getattr(record, "nino_detail", "-")),
            stage=str(getattr(record, "nino_stage", "-")),
            total=str(getattr(record, "nino_total", "-")),
            stage_s=getattr(record, "nino_stage_s", None),
            total_s=getattr(record, "nino_total_s", None),
            status=getattr(record, "nino_status", None),
            use_colors=colors_enabled(self.use_colors),
        )


def pipeline_log(
    kind: str,
    event: str,
    *,
    device_id: str | None = None,
    turn: object = None,
    session: str | None = None,
    t0: float | None = None,
    stage_s: float | None = None,
    **fields: Any,
) -> None:
    """One line: kind/event, details, then stage latency and overall total."""
    ctx_device, ctx_turn, ctx_session, ctx_t0 = _ctx()
    device = (device_id or ctx_device or "-").strip() or "-"
    used_turn = ctx_turn if turn is None else turn
    used_session = session if session is not None else ctx_session
    used_t0 = t0 if t0 is not None else ctx_t0
    total_s = (time.perf_counter() - used_t0) if used_t0 else None
    turn_label = "-" if used_turn is None else used_turn

    parts: list[str] = []
    if used_session:
        parts.append(f"session={used_session}")
    for key, value in fields.items():
        if value is None or value == "":
            continue
        text = str(value)
        if key in {"text", "prompt", "reply", "heard"} or any(ch.isspace() for ch in text):
            text = repr(_clip(text))
        else:
            text = _clip(text, 160)
        parts.append(f"{key}={text}")
    detail = " ".join(parts)
    label = f"{kind} {event}".strip()
    stage_txt = _seconds(stage_s)
    total_txt = _seconds(total_s)
    logger.info(
        "NINO | device=%s turn=%s | %-16s | %s | stage=%s total=%s",
        device,
        turn_label,
        label[:16],
        detail or "-",
        stage_txt,
        total_txt,
        extra={
            "nino_kind": kind,
            "nino_event": event,
            "nino_device": device,
            "nino_turn": turn_label,
            "nino_detail": detail or "-",
            "nino_stage": stage_txt,
            "nino_total": total_txt,
            "nino_stage_s": stage_s,
            "nino_total_s": total_s,
            "nino_status": fields.get("status"),
        },
    )


def log_http(
    kind: str,
    method: str,
    url: str,
    *,
    status: object = None,
    stage_s: float | None = None,
    extra: str = "",
    **fields: Any,
) -> None:
    payload = {
        "method": method.upper(),
        "host": _host(url),
        "status": status,
        **fields,
    }
    if extra:
        payload["detail"] = extra
    pipeline_log(kind, "HTTP", stage_s=stage_s, **payload)


class UvicornPollFilter(logging.Filter):
    """Hide dashboard polling so voice/cloud lines stay readable."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(path in msg for path in _NOISY_ACCESS_PATHS)


def uvicorn_log_config() -> dict:
    """Uvicorn's default config plus app INFO logs on the same console.

    Without a root logger, uvicorn only prints its own ``INFO:`` access lines.
    App loggers stay at WARNING, so ``NINO |`` never appears in the terminal.
    Pipeline lines use their own handler so they are not prefixed with ``INFO:``.
    """
    from copy import deepcopy

    from uvicorn.config import LOGGING_CONFIG

    config = deepcopy(LOGGING_CONFIG)
    config["disable_existing_loggers"] = False
    config.setdefault("formatters", {})
    config.setdefault("handlers", {})
    config.setdefault("loggers", {})
    config["formatters"]["nino"] = {
        "()": "pipeline_log.PipelineFormatter",
        "use_colors": None,
    }
    config["handlers"]["nino"] = {
        "formatter": "nino",
        "class": "logging.StreamHandler",
        "stream": "ext://sys.stderr",
    }
    config["loggers"][""] = {
        "handlers": ["default"],
        "level": "INFO",
    }
    config["loggers"]["nino.pipeline"] = {
        "handlers": ["nino"],
        "level": "INFO",
        "propagate": False,
    }
    return config
