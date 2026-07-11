from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path

os.environ.setdefault("OPENCV_LOG_LEVEL", "SILENT")

import cv2
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.staticfiles import StaticFiles as StarletteStaticFiles

from alarm_service import get_alarm_service
from camera import CameraStream
from eye_expression import normalize_eye_expression
from face_registration_service import capture_face_samples, configure_face_registration
from face_service import FaceService
from memory_service import configure_from_environ as configure_memory_from_environ
from memory_service import get_memory_service, normalize_database_url
from esp_playback import ensure_esp_play_wav_url_configured, esp_play_wav_url
from emotion_service import EmotionService
from tts_service import TTSService, synthesize_sapi_wav_bytes

logger = logging.getLogger(__name__)


def _load_env_file() -> None:
    try:
        from dotenv import load_dotenv

        env_path = Path(__file__).resolve().parent / ".env"
        if env_path.is_file():
            load_dotenv(env_path)
    except ImportError:
        pass


_load_env_file()

_voice_query_lock = threading.Lock()
_voice_query_depth = 0


def begin_voice_query() -> None:
    global _voice_query_depth
    with _voice_query_lock:
        _voice_query_depth += 1


def end_voice_query() -> None:
    global _voice_query_depth
    with _voice_query_lock:
        _voice_query_depth = max(0, _voice_query_depth - 1)


def voice_pipeline_active() -> bool:
    with _voice_query_lock:
        return _voice_query_depth > 0


class _GracefulShutdownFilter(logging.Filter):
    """Suppress expected Ctrl+C / CancelledError tracebacks during uvicorn shutdown."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_info and record.exc_info[0] is not None:
            exc_type = record.exc_info[0]
            if exc_type.__name__ in {"KeyboardInterrupt", "CancelledError"}:
                return False
            exc_val = record.exc_info[1]
            while exc_val is not None:
                if isinstance(exc_val, KeyboardInterrupt):
                    return False
                if isinstance(exc_val, asyncio.CancelledError):
                    return False
                exc_val = exc_val.__cause__ or exc_val.__context__
        if record.levelno >= logging.ERROR:
            msg = record.getMessage()
            if "CancelledError" in msg or "KeyboardInterrupt" in msg:
                return False
        return True


def _configure_shutdown_logging() -> None:
    filt = _GracefulShutdownFilter()
    logging.getLogger().addFilter(filt)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "starlette", "fastapi"):
        logging.getLogger(name).addFilter(filt)


async def _serve_uvicorn(host: str, port: int) -> None:
    import uvicorn

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="info",
        timeout_graceful_shutdown=3,
    )
    server = uvicorn.Server(config)
    await server.serve()


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "server_config.json"


def _load_server_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


SERVER_CONFIG = _load_server_config()
DEFAULT_CAMERA_SOURCE = os.getenv(
    "CAMERA_SOURCE",
    os.getenv("CAMERA_STREAM_URL", SERVER_CONFIG.get("camera_source", "auto")),
)
DEFAULT_ESP_PLAY_WAV_URL = os.getenv(
    "ESP_PLAY_WAV_URL", SERVER_CONFIG.get("esp_play_wav_url", "")
)

# Precedence: CLI flag > env var > server_config.json.
_CONFIG_ELEVENLABS_KEY = str(SERVER_CONFIG.get("elevenlabs_api_key", "")).strip()
if _CONFIG_ELEVENLABS_KEY and not os.environ.get("ELEVENLABS_API_KEY", "").strip():
    os.environ["ELEVENLABS_API_KEY"] = _CONFIG_ELEVENLABS_KEY

app = FastAPI(title="NiNO Camera Face Server")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


class NoCacheStaticFiles(StarletteStaticFiles):
    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        return response


app.mount("/static", NoCacheStaticFiles(directory=str(BASE_DIR / "static")), name="static")

camera = CameraStream(DEFAULT_CAMERA_SOURCE)
faces = FaceService(BASE_DIR / "data")
face_registration = configure_face_registration(faces, camera.read)
emotion = EmotionService()
_tts_face_interval = float(os.environ.get("FACE_GREETING_INTERVAL_SECONDS", "600"))
tts = TTSService(cooldown_seconds=0.0, face_greeting_interval_seconds=_tts_face_interval)
latest_results: list[dict] = []
# Update vision-driven TTS every frame so greetings start as soon as recognition succeeds
# (throttling here added a noticeable delay before enqueue).
TTS_UPDATE_INTERVAL_SECONDS = 0.0

# Remember who was recognized for voice follow-ups (survives brief detection gaps).
_voice_viewer_lock = threading.Lock()
_voice_viewer_name: str | None = None
_voice_viewer_at: float = 0.0
VOICE_VIEWER_TTL_SECONDS = float(os.environ.get("VOICE_VIEWER_TTL_SECONDS", "120"))


class CameraRequest(BaseModel):
    source: str = Field(..., min_length=1)


class RegisterRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    samples: int = Field(15, ge=1, le=80)
    interval_ms: int = Field(150, ge=50, le=2000)


class AlarmAckRequest(BaseModel):
    response: str = Field(..., min_length=1, max_length=32)


@app.on_event("startup")
def startup() -> None:
    _load_env_file()
    faces.apply_settings_from_environ()
    face_registration.apply_settings_from_environ()
    configure_memory_from_environ()
    get_memory_service().startup()
    get_alarm_service().start()
    ensure_esp_play_wav_url_configured()
    play_url = esp_play_wav_url()
    if play_url:
        logger.info("ESP speaker/eyes playback URL: %s", play_url)
    else:
        logger.warning(
            "ESP_PLAY_WAV_URL not set — face greeting TTS will not reach the board. "
            "Set ESP_PLAY_WAV_URL or CAMERA_SOURCE=http://<ESP_IP>/stream"
        )
    from network_util import voice_ws_url_for_esp

    ws_url = voice_ws_url_for_esp()
    if ws_url:
        logger.info("ESP voice WebSocket URL (sent on prompt_ack): %s", ws_url)
    else:
        logger.warning(
            "Could not derive VOICE_WS_URL — set VOICE_WS_URL or NINO_SERVER_LAN_HOST "
            "so face-registration listen can reach this PC"
        )
    camera.start()
    import threading

    from llm_service import warm_ollama_model

    threading.Thread(
        target=warm_ollama_model,
        kwargs={
            "model": os.environ.get("OLLAMA_MODEL"),
            "api_url": os.environ.get("OLLAMA_URL"),
        },
        daemon=True,
        name="ollama-warmup",
    ).start()
    stats = faces.stats()
    if not faces.recognizer_available:
        logger.warning(
            "opencv-contrib face module missing — install opencv-contrib-python for recognition"
        )
    elif stats["trained_people"] == 0 and stats["people"]:
        logger.warning("Face samples on disk but model not trained — click Retrain on the web UI")
    else:
        logger.info(
            "Face recognition ready: %d trained, threshold %.3f (soft %.3f), margin %.3f, "
            "confirm %d frames, detector=%s — Retrain after changes; add 20+ Chakri samples",
            stats["trained_people"],
            stats["threshold"],
            stats.get("soft_threshold", stats["threshold"]),
            stats.get("margin_min", 14),
            stats.get("confirm_frames", 5),
            stats.get("detector", "haar"),
        )


@app.on_event("shutdown")
def shutdown() -> None:
    try:
        get_memory_service().stop()
    except BaseException:
        pass
    try:
        get_alarm_service().stop()
    except BaseException:
        pass
    try:
        tts.stop()
    except BaseException:
        pass
    try:
        camera.stop()
    except BaseException as exc:
        print(f"Camera shutdown warning: {exc}")


@app.get("/")
def index(request: Request):
    response = templates.TemplateResponse(
        request,
        "index.html",
        {"camera_source": camera.source},
    )
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response


@app.post("/api/camera")
def set_camera(req: CameraRequest) -> dict:
    camera.restart(req.source)
    return {"ok": True, "camera": camera.status()}


@app.get("/api/latency-log")
def latency_log(limit: int = 50) -> dict:
    """Return recent voice latency records (newest last)."""
    limit = max(1, min(limit, 500))
    records = _load_latency_records()
    return {
        "ok": True,
        "total": len(records),
        "records": records[-limit:],
        "path": str(LATENCY_LOG_PATH),
    }


@app.get("/api/status")
def status() -> dict:
    from llm_service import ollama_runtime_status

    return {
        "camera": camera.status(),
        "faces": faces.stats(),
        "emotion": emotion.stats(),
        "voice_pipeline_active": voice_pipeline_active(),
        "tts": tts.status(),
        "llm": ollama_runtime_status(
            model=os.environ.get("OLLAMA_MODEL"),
            api_url=os.environ.get("OLLAMA_URL"),
        ),
        "alarms": get_alarm_service().status(),
        "memory": get_memory_service().status(),
        "face_registration": face_registration.status(),
        "latest_results": latest_results,
    }


@app.get("/api/memory/stats")
def memory_stats() -> dict:
    """PostgreSQL row counts — useful when validating Phase A/B/C."""
    return {"ok": True, **get_memory_service().table_stats()}


@app.get("/api/alarms")
def list_alarms() -> dict:
    return {"ok": True, **get_alarm_service().status()}


@app.delete("/api/alarms")
def cancel_all_alarms() -> dict:
    count = get_alarm_service().cancel_all()
    return {"ok": True, "cancelled": count}


@app.delete("/api/alarms/{alarm_id}")
def cancel_alarm(alarm_id: str) -> dict:
    if not get_alarm_service().cancel_alarm(alarm_id):
        raise HTTPException(status_code=404, detail="Alarm not found")
    return {"ok": True, "id": alarm_id}


@app.post("/api/alarms/{alarm_id}/ack")
def ack_alarm(alarm_id: str, req: AlarmAckRequest) -> dict:
    """Confirm or decline a medical alarm awaiting ack (same as voice yes/no)."""
    from alarm_medical import is_negative_ack, is_positive_ack

    service = get_alarm_service()
    text = req.response.strip()
    if is_positive_ack(text):
        if not service.confirm_ack(alarm_id):
            raise HTTPException(status_code=404, detail="No medical alarm awaiting confirmation")
        return {"ok": True, "id": alarm_id, "action": "confirmed"}
    if is_negative_ack(text):
        if not service.decline_ack(alarm_id):
            raise HTTPException(status_code=404, detail="No medical alarm awaiting confirmation")
        return {
            "ok": True,
            "id": alarm_id,
            "action": "declined",
            "message": "Reschedule or cancel? Use voice or set a new time on the server.",
        }
    raise HTTPException(
        status_code=400,
        detail="Use response yes or no (or I took it / not yet)",
    )


@app.post("/api/register")
def register(req: RegisterRequest) -> dict:
    probe = camera.read()
    if probe is not None:
        allowed, existing = faces.validate_registration_name(probe, req.name)
        if not allowed and existing:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"You're already registered as {existing}. "
                    "Use the same name to add more samples, or retrain only."
                ),
            )

    capture = capture_face_samples(
        faces,
        camera.read,
        req.name,
        samples=req.samples,
        interval_ms=req.interval_ms,
    )
    if capture.saved_samples == 0:
        detail = capture.errors[-1] if capture.errors else "No samples saved"
        if detail.startswith("already_registered_as:"):
            existing = detail.split(":", 1)[1]
            raise HTTPException(
                status_code=409,
                detail=(
                    f"You're already registered as {existing}. "
                    "Use the same name to add more samples."
                ),
            )
        raise HTTPException(
            status_code=422,
            detail=capture.errors[-1] if capture.errors else "No samples saved",
        )
    return {
        "ok": True,
        "saved_samples": capture.saved_samples,
        "training": capture.training,
        "last_error": capture.errors[-1] if capture.errors else "",
    }


@app.post("/api/retrain")
def retrain() -> dict:
    try:
        return {"ok": True, "training": faces.train()}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/snapshot.jpg")
def snapshot() -> Response:
    frame = camera.read()
    if frame is None:
        raise HTTPException(status_code=503, detail="No camera frame available")

    results = faces.recognize(frame)
    try:
        emotion.attach_emotions(frame, results)
    except Exception:
        logger.exception("Emotion detection failed for snapshot")
    annotated, _ = faces.annotate(frame, results=results)
    ok, encoded = cv2.imencode(".jpg", annotated)
    if not ok:
        raise HTTPException(status_code=500, detail="Could not encode snapshot")

    return Response(content=encoded.tobytes(), media_type="image/jpeg")


@app.get("/video_feed")
def video_feed() -> StreamingResponse:
    return StreamingResponse(
        _mjpeg_generator(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


def _mjpeg_generator():
    global latest_results
    last_tts_update_at = 0.0
    try:
        while True:
            frame = camera.read()
            if frame is None:
                tts.update_face_state([])
                time.sleep(0.02)
                continue

            results = faces.recognize(frame)
            try:
                emotion.attach_emotions(frame, results)
            except Exception:
                logger.exception("Emotion detection failed; continuing MJPEG")
            annotated, results = faces.annotate(frame, results=results)
            latest_results = results
            try:
                face_registration.on_frame(
                    results,
                    voice_active=voice_pipeline_active(),
                )
            except Exception:
                logger.exception("Face registration frame hook failed")
            now = time.time()
            if now - last_tts_update_at >= TTS_UPDATE_INTERVAL_SECONDS:
                _update_tts_face_state(results)
                last_tts_update_at = now

            ok, encoded = cv2.imencode(
                ".jpg", annotated, [int(cv2.IMWRITE_JPEG_QUALITY), 70]
            )
            if not ok:
                time.sleep(0.01)
                continue

            payload = encoded.tobytes()
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n"
                + f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii")
                + payload
                + b"\r\n"
            )
    except (GeneratorExit, BrokenPipeError, ConnectionResetError, KeyboardInterrupt):
        return


def _remember_voice_viewer(name: str | None) -> None:
    global _voice_viewer_name, _voice_viewer_at
    if not name:
        return
    cleaned = str(name).strip()
    if not cleaned or cleaned.lower() in {"unknown", "face"}:
        return
    with _voice_viewer_lock:
        _voice_viewer_name = cleaned
        _voice_viewer_at = time.time()


def _recall_voice_viewer() -> str | None:
    with _voice_viewer_lock:
        if not _voice_viewer_name:
            return None
        if time.time() - _voice_viewer_at > VOICE_VIEWER_TTL_SECONDS:
            return None
        return _voice_viewer_name


def _update_tts_face_state(results: list[dict]) -> None:
    recognized_names: list[str] = []
    for result in results:
        if not result.get("primary", True):
            continue
        if not result.get("stabilized"):
            continue
        name = str(result.get("name", "")).strip()
        if name and name.lower() not in {"unknown", "face"} and "[hold]" not in name:
            recognized_names.append(name)
    primary = _primary_recognized_viewer(results)
    tts.update_face_state(recognized_names, primary_name=primary)
    if primary:
        _remember_voice_viewer(primary)


def _primary_recognized_viewer(results: list[dict]) -> str | None:
    """Largest primary face in frame with a confident identity."""
    return faces.primary_viewer(results)


def _viewer_for_voice_query() -> str | None:
    """Who is speaking — live frame, recent stream results, then session memory."""
    name: str | None = None

    frame = camera.read()
    if frame is not None:
        name = _primary_recognized_viewer(faces.recognize(frame))

    if not name and latest_results:
        name = _primary_recognized_viewer(latest_results)

    if not name:
        name = tts.current_viewer_name()

    if not name:
        name = tts.viewer_name_for_voice()

    if not name:
        name = _recall_voice_viewer()

    if name:
        _remember_voice_viewer(name)

    return name


def _camera_identity_snapshot(
    require_live_face: bool = False,
) -> tuple[str | None, Literal["recognized", "unknown", "no_face"]]:
    """Live camera identity for voice queries and 'who am I?' prompts."""
    # Prefer the same live detection stream shown in UI so voice identity follows
    # what the user sees in the camera overlay.
    if latest_results:
        primary = _primary_recognized_viewer(latest_results)
        if primary:
            _remember_voice_viewer(primary)
            return primary, "recognized"
        if require_live_face:
            has_primary_face = any(r.get("primary", True) for r in latest_results)
            if has_primary_face:
                return None, "unknown"

    # Fresh frame fallback (keeps identity responsive even if latest_results lags).
    frame = camera.read()
    if frame is not None:
        results = faces.recognize(frame)
        primary = _primary_recognized_viewer(results)
        if primary:
            _remember_voice_viewer(primary)
            return primary, "recognized"
        if require_live_face and results:
            return None, "unknown"

    # Multi-frame vote fallback.
    name, state = faces.recognize_identity(
        camera.read,
        allow_session_hint=not require_live_face,
    )
    if state == "recognized" and name:
        _remember_voice_viewer(name)
        return name, "recognized"
    if state == "no_face":
        if require_live_face:
            return None, "no_face"
        recalled = _recall_voice_viewer()
        if recalled:
            return recalled, "recognized"
        return None, "no_face"
    return None, "unknown"


async def _delayed_esp_servo_360(delay_seconds: float) -> None:
    from voice_service import SERVO_360_TRIGGER_DELAY_SECONDS, trigger_esp_servo_360

    wait = delay_seconds if delay_seconds > 0 else SERVO_360_TRIGGER_DELAY_SECONDS
    await asyncio.sleep(wait)
    ok, err = await run_in_threadpool(trigger_esp_servo_360)
    if ok:
        logger.info("ESP servo 360 started after voice confirmation")
    else:
        logger.warning("ESP servo 360 failed after voice confirmation: %s", err or "unknown")


async def _delayed_face_reg_relisten(delay_seconds: float) -> None:
    wait = max(0.0, delay_seconds)
    if wait:
        await asyncio.sleep(wait)
    await run_in_threadpool(face_registration.relisten_after_missed_name)


LATENCY_LOG_PATH = BASE_DIR / "data" / "latency_log.json"
_LATENCY_LOG_LOCK = threading.Lock()


def _load_latency_records() -> list[dict]:
    if not LATENCY_LOG_PATH.exists():
        return []
    try:
        with open(LATENCY_LOG_PATH, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, list):
            return [item for item in loaded if isinstance(item, dict)]
        logger.warning("latency_log.json was not a JSON array; starting a new log.")
        return []
    except json.JSONDecodeError as exc:
        backup = LATENCY_LOG_PATH.with_name(
            f"latency_log.corrupt.{datetime.now().strftime('%Y%m%d%H%M%S')}.json"
        )
        try:
            LATENCY_LOG_PATH.rename(backup)
        except OSError:
            pass
        logger.error(
            "latency_log.json was corrupt (%s); backed up to %s",
            exc,
            backup.name,
        )
        return []


def _append_latency_record(record: dict) -> None:
    """Append one record to data/latency_log.json (thread-safe, atomic write)."""
    with _LATENCY_LOG_LOCK:
        tmp_path = LATENCY_LOG_PATH.with_suffix(".json.tmp")
        try:
            records = _load_latency_records()
            records.append(record)
            LATENCY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(records, f, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, LATENCY_LOG_PATH)
        except Exception:
            logger.exception("Failed to write latency log")
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass


def _latency_log_record(**fields: object) -> dict:
    return {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        **fields,
    }


def _ws_client_label(websocket: WebSocket) -> str:
    client = websocket.client
    if client is None:
        return "unknown"
    host = getattr(client, "host", None) or "unknown"
    port = getattr(client, "port", None)
    return f"{host}:{port}" if port is not None else str(host)


async def _voice_ws_pipeline(websocket: WebSocket) -> None:
    """Whisper STT → Ollama → SAPI WAV. Multiple receive_bytes → send_bytes cycles per connection."""
    await websocket.accept()
    from voice_service import (
        SERVO_360_TRIGGER_DELAY_SECONDS,
        VoiceReplyMeta,
        minimal_voice_reply_wav,
        process_voice_wav,
    )

    session_queries = 0
    session_started = time.perf_counter()
    client_label = _ws_client_label(websocket)
    await run_in_threadpool(
        _append_latency_record,
        _latency_log_record(event="ws_open", client=client_label),
    )

    try:
        while True:
            try:
                message = await websocket.receive()
            except WebSocketDisconnect:
                break
            if message.get("type") == "websocket.disconnect":
                break
            if message.get("text") is not None:
                await run_in_threadpool(
                    _append_latency_record,
                    _latency_log_record(
                        event="ws_text_frame",
                        client=client_label,
                        text=str(message["text"])[:200],
                    ),
                )
                continue
            wav_in = message.get("bytes")
            if wav_in is None:
                await run_in_threadpool(
                    _append_latency_record,
                    _latency_log_record(
                        event="ws_empty_frame",
                        client=client_label,
                        error="WebSocket frame had no audio bytes.",
                    ),
                )
                continue

            wav_out: bytes | None = None
            reply_meta = VoiceReplyMeta()
            active_viewer: str | None = None
            t_query_start = time.perf_counter()
            pipeline_error: str | None = None
            await run_in_threadpool(begin_voice_query)
            await run_in_threadpool(face_registration.on_voice_query_started)
            try:
                try:
                    identity_name, identity_state = await run_in_threadpool(
                        _camera_identity_snapshot, True
                    )
                    if identity_state == "recognized" and identity_name:
                        normalized_identity = str(identity_name).strip()
                        if normalized_identity and normalized_identity.lower() not in {
                            "unknown",
                            "face",
                        }:
                            active_viewer = normalized_identity

                    def _run_voice() -> tuple[bytes, VoiceReplyMeta]:
                        return process_voice_wav(
                            wav_in,
                            active_viewer,
                            camera_identity_name=identity_name,
                            camera_identity_state=identity_state,
                        )

                    wav_out, reply_meta = await run_in_threadpool(_run_voice)
                except Exception as exc:
                    logger.exception("Voice pipeline failed")
                    pipeline_error = str(exc)[:200]
                    err_note = str(exc)[:512]
                    try:
                        from llm_service import (
                            OLLAMA_UNAVAILABLE_REPLY,
                            brief_spoken_message,
                            is_ollama_error,
                        )
                        from wav_resample import ESP_PCM_SAMPLE_RATE_HZ, resample_wav_bytes_to_mono_16bit

                        def _recover_wav(pipeline_exc: BaseException = exc) -> bytes:
                            if is_ollama_error(pipeline_exc):
                                txt = OLLAMA_UNAVAILABLE_REPLY
                            else:
                                txt = brief_spoken_message(
                                    err_note,
                                    model=os.environ.get("OLLAMA_MODEL"),
                                    api_url=os.environ.get("OLLAMA_URL"),
                                )
                            raw, _ = synthesize_sapi_wav_bytes(txt)
                            return resample_wav_bytes_to_mono_16bit(raw, ESP_PCM_SAMPLE_RATE_HZ)

                        wav_out = await run_in_threadpool(_recover_wav)
                    except Exception:
                        logger.exception("Voice pipeline recovery failed")
                        wav_out = minimal_voice_reply_wav()

                if not wav_out and not reply_meta.face_reg_relisten:
                    wav_out = minimal_voice_reply_wav()

                timings = dict(reply_meta.timings)
                timings.setdefault("audio_in_bytes", len(wav_in))
                record = _latency_log_record(
                    event="voice_query",
                    client=client_label,
                    server_total_seconds=round(time.perf_counter() - t_query_start, 3),
                    **timings,
                )
                if pipeline_error is not None:
                    record["error"] = pipeline_error
                await run_in_threadpool(_append_latency_record, record)
                session_queries += 1

                if reply_meta.registered_face_name:
                    _remember_voice_viewer(reply_meta.registered_face_name)

                await run_in_threadpool(tts.notify_voice_interaction, active_viewer)

                ws_meta: dict[str, object] = {
                    "prompt_medical_ack": reply_meta.prompt_medical_ack,
                }
                eye_tag = normalize_eye_expression(reply_meta.eye_expression)
                if eye_tag:
                    ws_meta["eye_expression"] = eye_tag
                    logger.info(
                        "WS send eye_expression=%s client=%s",
                        eye_tag,
                        client_label,
                    )
                await websocket.send_json(ws_meta)
                if wav_out:
                    await websocket.send_bytes(wav_out)

                if reply_meta.face_reg_relisten:
                    asyncio.create_task(_delayed_face_reg_relisten(0.5))

                if reply_meta.trigger_servo_360:
                    asyncio.create_task(
                        _delayed_esp_servo_360(SERVO_360_TRIGGER_DELAY_SECONDS)
                    )
            except WebSocketDisconnect:
                await run_in_threadpool(
                    _append_latency_record,
                    _latency_log_record(
                        event="ws_disconnect_during_send",
                        client=client_label,
                        heard=reply_meta.timings.get("heard", "")[:200],
                    ),
                )
                break
            finally:
                await run_in_threadpool(end_voice_query)
    finally:
        if session_queries == 0:
            await run_in_threadpool(
                _append_latency_record,
                _latency_log_record(
                    event="ws_closed_without_audio",
                    client=client_label,
                    session_seconds=round(time.perf_counter() - session_started, 3),
                ),
            )
        else:
            await run_in_threadpool(
                _append_latency_record,
                _latency_log_record(
                    event="ws_closed",
                    client=client_label,
                    session_queries=session_queries,
                    session_seconds=round(time.perf_counter() - session_started, 3),
                ),
            )
        try:
            await websocket.close()
        except Exception:
            pass


@app.websocket("/ws/voice")
async def voice_websocket(websocket: WebSocket) -> None:
    await _voice_ws_pipeline(websocket)


@app.websocket("/voice-query")
async def voice_query_websocket(websocket: WebSocket) -> None:
    """Same pipeline as /ws/voice (ESP32 voice_optimized URI compatibility)."""
    await _voice_ws_pipeline(websocket)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the NiNO camera face server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", default=8000, type=int)
    parser.add_argument(
        "--camera-source",
        default=DEFAULT_CAMERA_SOURCE,
        help="Local camera index like 0/1, or a stream URL like http://host/stream",
    )
    parser.add_argument(
        "--camera-url",
        default=None,
        help="Backward compatible alias for --camera-source",
    )
    parser.add_argument(
        "--camera-rotation",
        default=os.environ.get("CAMERA_ROTATION", ""),
        help="Rotate each frame before detection/display: cw90, ccw90, 180, or none",
    )
    parser.add_argument(
        "--esp-play-wav-url",
        default=DEFAULT_ESP_PLAY_WAV_URL,
        help="POST synthesized WAV to this URL for ESP32-P4 speaker (e.g. http://IP/play_wav)",
    )
    parser.add_argument(
        "--ollama-url",
        default=os.environ.get("OLLAMA_URL", "auto"),
        help="Ollama generate API URL (default: auto — prefer GPU on :11435)",
    )
    parser.add_argument(
        "--ollama-model",
        default=os.environ.get("OLLAMA_MODEL", "qwen2.5:1.5b"),
        help="Ollama model name (e.g. qwen2.5:1.5b)",
    )
    parser.add_argument(
        "--whisper-model",
        default=os.environ.get("WHISPER_MODEL", "small"),
        help="faster-whisper model for /ws/voice (tiny, base, small, ...)",
    )
    parser.add_argument(
        "--stt-provider",
        default=os.environ.get("STT_PROVIDER", ""),
        choices=["", "whisper", "elevenlabs"],
        help="STT engine: elevenlabs (cloud Scribe, needs API key) or whisper (local). "
        "Default: elevenlabs when an API key is set, else whisper.",
    )
    parser.add_argument(
        "--elevenlabs-api-key",
        default=os.environ.get("ELEVENLABS_API_KEY", ""),
        help="ElevenLabs API key for cloud STT/TTS (or set ELEVENLABS_API_KEY env var)",
    )
    parser.add_argument(
        "--tts-provider",
        default=os.environ.get("TTS_PROVIDER", ""),
        choices=["", "elevenlabs", "sapi", "local"],
        help="TTS engine: elevenlabs (cloud, default when API key set), sapi (Windows), or local (espeak-ng)",
    )
    parser.add_argument(
        "--face-greeting-interval",
        type=float,
        default=float(os.environ.get("FACE_GREETING_INTERVAL_SECONDS", "600")),
        metavar="SEC",
        help="Min seconds between vision welcome-backs for the same person (first sighting still greets once). Default 600 (10 min); use 300 for 5 min.",
    )
    parser.add_argument(
        "--face-threshold",
        type=float,
        default=None,
        metavar="LBPH",
        help="Max LBPH distance for strict green (lower=stricter). Default 62; per-person cap after Retrain.",
    )
    parser.add_argument(
        "--face-margin",
        type=float,
        default=None,
        metavar="LBPH",
        help="Min LBPH gap vs 2nd person (default 10). Lower = stricter anti mix-up.",
    )
    parser.add_argument(
        "--face-confirm-frames",
        type=int,
        default=None,
        metavar="N",
        help="Frames to stabilize name for voice (UI green is immediate on strict match). Default 3.",
    )
    parser.add_argument(
        "--alarm-wav",
        default=os.environ.get("ALARM_WAV_PATH", ""),
        help="WAV file POSTed to ESP when an alarm fires (default: ../main/beep.wav)",
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL", ""),
        help="PostgreSQL URL for conversation memory (e.g. postgresql://nino:nino@127.0.0.1:5432/nino_memory)",
    )
    parser.add_argument(
        "--memory-extraction",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Phase B: extract long-term facts after each logged turn "
        "(default: on when DATABASE_URL is set)",
    )
    parser.add_argument(
        "--memory-min-importance",
        type=int,
        default=None,
        metavar="N",
        help="Min importance 0-10 for storing memories (default 5)",
    )
    parser.add_argument(
        "--memory-top-memories",
        type=int,
        default=None,
        metavar="N",
        help="Max long-term facts loaded before each reply (default 10)",
    )
    args = parser.parse_args()

    _load_env_file()

    env_db = normalize_database_url(os.environ.get("DATABASE_URL", ""))
    cli_db = normalize_database_url(args.database_url.strip())
    if cli_db:
        os.environ["DATABASE_URL"] = cli_db
    elif args.database_url.strip() and env_db:
        logger.warning(
            "Ignoring invalid --database-url %r; using DATABASE_URL from environment",
            args.database_url.strip()[:60],
        )
        os.environ["DATABASE_URL"] = env_db
    elif env_db:
        os.environ["DATABASE_URL"] = env_db
    elif args.database_url.strip():
        os.environ["DATABASE_URL"] = args.database_url.strip()

    resolved_db = normalize_database_url(os.environ.get("DATABASE_URL", ""))
    if args.memory_extraction is not None:
        os.environ["MEMORY_EXTRACTION"] = "1" if args.memory_extraction else "0"
    elif resolved_db and "MEMORY_EXTRACTION" not in os.environ:
        os.environ["MEMORY_EXTRACTION"] = "1"
    if args.memory_min_importance is not None:
        os.environ["MEMORY_MIN_IMPORTANCE"] = str(max(0, min(10, args.memory_min_importance)))
    if args.memory_top_memories is not None:
        os.environ["MEMORY_TOP_MEMORIES"] = str(max(1, args.memory_top_memories))

    camera_source = args.camera_url or args.camera_source
    os.environ["CAMERA_SOURCE"] = camera_source
    camera.source = camera_source
    if args.camera_rotation.strip():
        os.environ["CAMERA_ROTATION"] = args.camera_rotation.strip()

    if args.esp_play_wav_url.strip():
        os.environ["ESP_PLAY_WAV_URL"] = args.esp_play_wav_url.strip()
    ensure_esp_play_wav_url_configured(camera_source=camera_source)

    if args.alarm_wav.strip():
        os.environ["ALARM_WAV_PATH"] = args.alarm_wav.strip()

    from llm_service import resolve_ollama_api_url, try_start_gpu_ollama

    try_start_gpu_ollama()
    ollama_url = args.ollama_url.strip()
    if not ollama_url or ollama_url.lower() in {"auto", "detect"}:
        ollama_url = resolve_ollama_api_url(model=args.ollama_model.strip())
    os.environ["OLLAMA_URL"] = ollama_url
    os.environ["OLLAMA_MODEL"] = args.ollama_model.strip()
    os.environ["WHISPER_MODEL"] = args.whisper_model.strip()
    if args.stt_provider.strip():
        os.environ["STT_PROVIDER"] = args.stt_provider.strip().lower()
    if args.elevenlabs_api_key.strip():
        os.environ["ELEVENLABS_API_KEY"] = args.elevenlabs_api_key.strip()
    if args.tts_provider.strip():
        os.environ["TTS_PROVIDER"] = args.tts_provider.strip().lower()

    if args.face_threshold is not None:
        os.environ["FACE_RECOGNITION_THRESHOLD"] = str(args.face_threshold)
    if args.face_margin is not None:
        os.environ["FACE_RECOGNITION_MARGIN"] = str(args.face_margin)
    if args.face_confirm_frames is not None:
        os.environ["FACE_CONFIRM_FRAMES"] = str(max(1, args.face_confirm_frames))

    tts.face_greeting_interval_seconds = max(1.0, float(args.face_greeting_interval))

    tts.configure_llm(
        ollama_url=ollama_url,
        ollama_model=args.ollama_model.strip(),
    )

    from voice_service import configure_from_environ

    configure_from_environ()
    configure_memory_from_environ()
    get_memory_service().startup()
    _configure_shutdown_logging()

    try:
        asyncio.run(_serve_uvicorn(args.host, args.port))
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        shutdown()
    logger.info("Server stopped.")


if __name__ == "__main__":
    main()
