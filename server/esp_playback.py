"""POST WAV audio to the ESP32 /play_wav endpoint."""

from __future__ import annotations

import io
import json
import logging
import os
import struct
import threading
import time
import wave
import urllib.error
import urllib.parse
import urllib.request

from network_util import voice_ws_url_for_esp
from pipeline_log import log_http, pipeline_log

logger = logging.getLogger(__name__)

# ESP32 main.c MAX_PLAY_WAV_BYTES — keep a little under for safety
ESP_MAX_PLAY_WAV_BYTES = int(os.environ.get("ESP_MAX_PLAY_WAV_BYTES", str(380 * 1024)))

# Extra seconds the device is considered "busy" after a WAV finishes (eye reverts to
# idle, ack chime, etc.). Keeps the vision-emotion eye driver from stomping on speech.
_EYE_BUSY_TAIL_SECONDS = float(os.environ.get("EYE_BUSY_TAIL_SECONDS", "1.5"))

# Extra guard after estimated clip end before streamed Aux PCM is accepted (room echo).
_PLAYBACK_BUSY_EXTRA_SECONDS = float(os.environ.get("VOICE_PLAYBACK_BUSY_EXTRA_SECONDS", "0.75"))

# HTTP upload skew — POST may return before the DAC starts; extend busy for large clips.
_PLAYBACK_UPLOAD_SKEW_BYTES_PER_SEC = float(
    os.environ.get("VOICE_PLAYBACK_UPLOAD_SKEW_BPS", "80000")
)

# After estimated TTS duration, keep dropping Aux PCM this long for I2S/room echo.
# Do not use VOICE_POST_TTS_GRACE_SECONDS here — that grace is for STT energy
# rejection, and adding it here makes the robot deaf for ~4s after playback.
_STREAM_ECHO_TAIL_SECONDS = float(os.environ.get("VOICE_STREAM_ECHO_TAIL_SECONDS", "0.25"))

# Shared "device is speaking" window. post_wav_to_esp() extends it so lower-priority
# eye updates (camera emotion) can yield to voice replies, alarms, and greetings.
_busy_lock = threading.Lock()
_device_busy_until: dict[str, float] = {}
_GLOBAL_BUSY_KEY = "__global__"


def _busy_key(device_id: str | None) -> str:
    from user_devices import normalize_device_mac

    mac = normalize_device_mac(device_id)
    if mac:
        return mac
    cleaned = str(device_id or "").strip()
    return cleaned or _GLOBAL_BUSY_KEY


def _wav_duration_seconds(wav: bytes) -> float:
    """Best-effort duration from a canonical PCM WAV header; 0.0 if unknown."""
    try:
        if len(wav) < 44 or wav[:4] != b"RIFF" or wav[8:12] != b"WAVE":
            return 0.0
        byte_rate = struct.unpack_from("<I", wav, 28)[0]
        if byte_rate <= 0:
            return 0.0
        return max(0.0, (len(wav) - 44) / float(byte_rate))
    except Exception:
        return 0.0


def stream_echo_tail_seconds() -> float:
    """Short tail after estimated TTS before streamed Aux PCM is accepted."""
    return max(0.0, _STREAM_ECHO_TAIL_SECONDS)


def playback_busy_seconds(wav: bytes, *, from_completion: bool = False) -> float:
    """How long to treat the device as speaking for this WAV."""
    play_s = _wav_duration_seconds(wav)
    tail = _EYE_BUSY_TAIL_SECONDS + stream_echo_tail_seconds() + _PLAYBACK_BUSY_EXTRA_SECONDS
    upload_skew = 0.0
    if from_completion and _PLAYBACK_UPLOAD_SKEW_BYTES_PER_SEC > 0:
        upload_skew = min(30.0, len(wav) / _PLAYBACK_UPLOAD_SKEW_BYTES_PER_SEC)
    return max(0.5, play_s + tail + upload_skew)


def extend_playback_busy(wav: bytes, device_id: str | None = None) -> float:
    """Extend busy window from now until playback should be finished on the speaker."""
    seconds = playback_busy_seconds(wav, from_completion=True)
    mark_device_busy_for(seconds, device_id=device_id)
    return seconds


def wait_device_playback_idle(device_id: str | None = None, *, timeout_s: float = 120.0) -> bool:
    """Block until the device is no longer in a TTS playback window."""
    deadline = time.time() + max(1.0, timeout_s)
    while time.time() < deadline:
        if not device_busy_speaking(device_id):
            return True
        time.sleep(0.15)
    return False


def mark_device_busy_for(seconds: float, device_id: str | None = None) -> None:
    """Extend the 'device is speaking' window to at least now + seconds."""
    key = _busy_key(device_id)
    until = time.time() + max(0.0, seconds)
    with _busy_lock:
        _device_busy_until[key] = max(_device_busy_until.get(key, 0.0), until)


def clear_device_busy(device_id: str | None = None) -> None:
    """Device finished TTS and is listening — accept Aux PCM immediately."""
    key = _busy_key(device_id)
    with _busy_lock:
        _device_busy_until.pop(key, None)


def device_busy_speaking(device_id: str | None = None) -> bool:
    """True while this ESP is (estimated to be) playing audio, plus a short tail."""
    now = time.time()
    with _busy_lock:
        if device_id:
            return now < _device_busy_until.get(_busy_key(device_id), 0.0)
        return any(now < until for until in _device_busy_until.values())


def esp_eye_expression_url(device_id: str | None = None) -> str | None:
    """Return the eye-expression endpoint for one device."""
    base = device_base_url(device_id)
    if not base:
        return None
    return f"{base.rstrip('/')}/eye/expression"


def post_eye_expression_to_esp(
    name: str, *, timeout: float = 3.0, device_id: str | None = None
) -> bool:
    """POST {"expression": name} to the ESP /eye/expression endpoint.

    Never raises — returns True on success, False otherwise (called from the
    camera frame loop where a failure must not break streaming).
    """
    expression = (name or "").strip().lower()
    if not expression:
        return False
    url = esp_eye_expression_url(device_id)
    if not url:
        logger.warning(
            "eye/expression skipped (%s): no endpoint for device_id=%s",
            expression,
            device_id or "<default>",
        )
        return False
    body = json.dumps({"expression": expression}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                return True
            logger.warning(
                "eye/expression rejected (%s) by device_id=%s: HTTP %s",
                expression,
                device_id or "<default>",
                resp.status,
            )
            return False
    except (urllib.error.URLError, OSError) as exc:
        logger.warning(
            "eye/expression POST failed (%s) for device_id=%s: %s",
            expression,
            device_id or "<default>",
            exc,
        )
        return False


def derive_esp_base_url(camera_source: str) -> str | None:
    """Extract http://host from CAMERA_SOURCE (e.g. http://192.168.0.89/stream)."""
    raw = (camera_source or "").strip()
    if not raw.lower().startswith(("http://", "https://")):
        return None
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return None


def ensure_esp_play_wav_url_configured(*, camera_source: str | None = None) -> str | None:
    """Set ESP_PLAY_WAV_URL from CAMERA_SOURCE or a known device when unset."""
    existing = os.environ.get("ESP_PLAY_WAV_URL", "").strip()
    if existing:
        return existing
    cam = camera_source if camera_source is not None else os.environ.get("CAMERA_SOURCE", "")
    base = derive_esp_base_url(cam)
    if base:
        url = f"{base}/play_wav"
        os.environ["ESP_PLAY_WAV_URL"] = url
        logger.info("ESP_PLAY_WAV_URL derived from camera source: %s", url)
        return url
    try:
        from device_registry import get_device_registry

        for record in get_device_registry().list_devices():
            url = record.effective_play_wav_url()
            if url:
                os.environ["ESP_PLAY_WAV_URL"] = url
                logger.info(
                    "ESP_PLAY_WAV_URL derived from device %s: %s",
                    record.device_id,
                    url,
                )
                return url
    except Exception:
        logger.debug("Could not derive ESP_PLAY_WAV_URL from device registry", exc_info=True)
    return None


def esp_play_wav_url() -> str | None:
    url = os.environ.get("ESP_PLAY_WAV_URL", "").strip()
    if url:
        return url
    return ensure_esp_play_wav_url_configured()


def split_wav_for_esp(wav: bytes, *, max_bytes: int | None = None) -> list[bytes]:
    """Split PCM WAV into sequential clips each <= max_bytes (frame-aligned)."""
    limit = ESP_MAX_PLAY_WAV_BYTES if max_bytes is None else max_bytes
    if len(wav) <= limit:
        return [wav]

    with wave.open(io.BytesIO(wav), "rb") as wf:
        params = wf.getparams()
        raw = wf.readframes(wf.getnframes())

    nch, sw, sr = params.nchannels, params.sampwidth, params.framerate
    frame_bytes = nch * sw
    if frame_bytes <= 0 or not raw:
        raise RuntimeError("Cannot split invalid or empty WAV for ESP")

    def _pack(frames: bytes) -> bytes:
        bio = io.BytesIO()
        with wave.open(bio, "wb") as wo:
            wo.setnchannels(nch)
            wo.setsampwidth(sw)
            wo.setframerate(sr)
            wo.writeframes(frames)
        return bio.getvalue()

    chunks: list[bytes] = []
    offset = 0
    total = len(raw)
    while offset < total:
        remaining = total - offset
        chunk_frames = min(
            remaining,
            max((limit - 64) // frame_bytes, 1) * frame_bytes,
        )
        while chunk_frames >= frame_bytes:
            packed = _pack(raw[offset : offset + chunk_frames])
            if len(packed) <= limit:
                chunks.append(packed)
                offset += chunk_frames
                break
            chunk_frames -= frame_bytes * 50
        else:
            single = _pack(raw[offset : offset + frame_bytes])
            raise RuntimeError(
                f"WAV frame too large for ESP ({len(single)} bytes; max {limit})"
            )
    return chunks


def _post_wav_chunk_to_url(
    url: str,
    wav: bytes,
    *,
    timeout: float = 60.0,
    prompt_ack: bool = False,
    prompt_ack_chime: bool = True,
    eye_expression: str | None = None,
    device_id: str | None = None,
    clip_index: int = 1,
    clip_total: int = 1,
) -> None:
    if len(wav) > ESP_MAX_PLAY_WAV_BYTES:
        raise RuntimeError(
            f"WAV too large for ESP ({len(wav)} bytes; max {ESP_MAX_PLAY_WAV_BYTES})"
        )

    headers = {"Content-Type": "audio/wav"}
    if prompt_ack:
        headers["X-Nino-Prompt-Ack"] = "1"
        headers["X-Nino-Prompt-Ack-Chime"] = "1" if prompt_ack_chime else "0"
        ws_url = voice_ws_url_for_esp(device_id=device_id)
        if ws_url:
            headers["X-Nino-Voice-Ws-Url"] = ws_url
        else:
            logger.warning(
                "prompt_ack without VOICE_WS_URL / NINO_SERVER_LAN_HOST — "
                "ESP may not reach PC voice WebSocket"
            )
    if eye_expression:
        headers["X-Nino-Eye-Expression"] = eye_expression.strip().lower()

    # Reserve the "speaking" window up front so the emotion eye driver yields
    # immediately, even while this (blocking) POST is still in flight.
    if clip_total == 1:
        mark_device_busy_for(
            playback_busy_seconds(wav, from_completion=False),
            device_id=device_id,
        )

    req = urllib.request.Request(
        url,
        data=wav,
        method="POST",
        headers=headers,
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                raise RuntimeError(f"ESP play_wav HTTP {resp.status}")
            body = resp.read()
            if b'"ok":true' not in body and b'"ok": true' not in body:
                if b'"ok":false' in body or b'"ok": false' in body:
                    raise RuntimeError(body.decode("utf-8", errors="replace"))
        extra = "play_wav"
        if clip_total > 1:
            extra = f"play_wav {clip_index}/{clip_total}"
        log_http(
            "DEVICE",
            "POST",
            url,
            status=200,
            stage_s=time.perf_counter() - t0,
            wav_bytes=len(wav),
            device=device_id or "-",
            extra=extra,
        )
    except urllib.error.HTTPError as exc:
        log_http(
            "DEVICE",
            "POST",
            url,
            status=exc.code,
            stage_s=time.perf_counter() - t0,
            extra=str(exc)[:120],
        )
        detail = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
        raise RuntimeError(f"ESP HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        log_http(
            "DEVICE",
            "POST",
            url,
            status="error",
            stage_s=time.perf_counter() - t0,
            extra=str(exc)[:120],
        )
        raise RuntimeError(f"ESP URL error: {exc}") from exc

    if clip_total == 1:
        extend_playback_busy(wav, device_id=device_id)


def _post_wav_to_url(
    url: str,
    wav: bytes,
    *,
    timeout: float = 60.0,
    prompt_ack: bool = False,
    prompt_ack_chime: bool = True,
    eye_expression: str | None = None,
    device_id: str | None = None,
) -> None:
    if not wav:
        raise RuntimeError("WAV payload is empty")

    chunks = split_wav_for_esp(wav)
    if len(chunks) > 1:
        logger.info(
            "ESP play_wav auto-split %d clips (%d bytes; max %d) device=%s",
            len(chunks),
            len(wav),
            ESP_MAX_PLAY_WAV_BYTES,
            device_id or "-",
        )
        total_busy = sum(
            playback_busy_seconds(chunk, from_completion=False) for chunk in chunks
        )
        mark_device_busy_for(total_busy, device_id=device_id)

    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        _post_wav_chunk_to_url(
            url,
            chunk,
            timeout=timeout,
            prompt_ack=prompt_ack and is_last,
            prompt_ack_chime=prompt_ack_chime,
            eye_expression=eye_expression if is_last else None,
            device_id=device_id,
            clip_index=i + 1,
            clip_total=len(chunks),
        )

    if len(chunks) > 1:
        extend_playback_busy(chunks[-1], device_id=device_id)


def post_wav_to_esp(
    wav: bytes,
    *,
    timeout: float = 60.0,
    prompt_ack: bool = False,
    prompt_ack_chime: bool = True,
    eye_expression: str | None = None,
) -> None:
    """Queue raw WAV bytes on the board speaker via POST /play_wav (legacy global URL)."""
    url = esp_play_wav_url()
    if not url:
        raise RuntimeError(
            "ESP_PLAY_WAV_URL is not set (set it or use CAMERA_SOURCE=http://<ESP_IP>/stream)"
        )
    _post_wav_to_url(
        url,
        wav,
        timeout=timeout,
        prompt_ack=prompt_ack,
        prompt_ack_chime=prompt_ack_chime,
        eye_expression=eye_expression,
    )


def deliver_wav_to_device(
    device_id: str | None,
    wav: bytes,
    *,
    timeout: float = 60.0,
    prompt_ack: bool = False,
    prompt_ack_chime: bool = True,
    eye_expression: str | None = None,
) -> None:
    """POST WAV to the play_wav URL for this MAC. Named MACs never steal another robot."""
    from device_registry import get_device_registry
    from user_devices import normalize_device_mac

    mac = normalize_device_mac(device_id)
    if mac:
        record = get_device_registry().get(mac)
        url = record.effective_play_wav_url() if record else None
        if not url:
            raise RuntimeError(
                f"No play_wav URL for device_id={mac!r} "
                "(set devices.json camera/play URLs for this MAC)"
            )
        play_id = record.device_id
    else:
        record = get_device_registry().resolve_or_default(device_id)
        url = record.effective_play_wav_url() or esp_play_wav_url()
        if not url:
            raise RuntimeError(
                f"No play_wav URL for device_id={record.device_id!r} "
                "(set devices.json or ESP_PLAY_WAV_URL / CAMERA_SOURCE)"
            )
        play_id = record.device_id
    logger.debug("deliver_wav_to_device device_id=%s url=%s", play_id, url)
    _post_wav_to_url(
        url,
        wav,
        timeout=timeout,
        prompt_ack=prompt_ack,
        prompt_ack_chime=prompt_ack_chime,
        eye_expression=eye_expression,
        device_id=play_id,
    )


def device_base_url(device_id: str | None) -> str | None:
    """HTTP base for volume/servo on a device."""
    from device_registry import get_device_registry
    from user_devices import normalize_device_mac

    mac = normalize_device_mac(device_id)
    if mac:
        record = get_device_registry().get(mac)
        return record.effective_base_url() if record else None

    base = get_device_registry().resolve_or_default(device_id).effective_base_url()
    if base:
        return base
    play = esp_play_wav_url()
    if play:
        parsed = urllib.parse.urlparse(play)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
    return None
