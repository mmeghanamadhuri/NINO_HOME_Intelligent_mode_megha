from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import threading
import time
from pathlib import Path

os.environ.setdefault("OPENCV_LOG_LEVEL", "SILENT")

import cv2
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request

from alarm_service import get_alarm_service
from camera import CameraStream
from face_service import FaceService
from tts_service import TTSService, synthesize_sapi_wav_bytes

logger = logging.getLogger(__name__)

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

app = FastAPI(title="NiNO Camera Face Server")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

camera = CameraStream(DEFAULT_CAMERA_SOURCE)
faces = FaceService(BASE_DIR / "data")
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
VOICE_VIEWER_TTL_SECONDS = float(os.environ.get("VOICE_VIEWER_TTL_SECONDS", "900"))


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
    faces.apply_settings_from_environ()
    get_alarm_service().start()
    camera.start()
    stats = faces.stats()
    if not faces.recognizer_available:
        logger.warning(
            "opencv-contrib face module missing — install opencv-contrib-python for recognition"
        )
    elif stats["trained_people"] == 0 and stats["people"]:
        logger.warning("Face samples on disk but model not trained — click Retrain on the web UI")
    else:
        logger.info(
            "Face recognition ready: %d trained, threshold %.0f (soft %.0f), margin %.0f, "
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
    return templates.TemplateResponse(
        request,
        "index.html",
        {"camera_source": camera.source},
    )


@app.post("/api/camera")
def set_camera(req: CameraRequest) -> dict:
    camera.restart(req.source)
    return {"ok": True, "camera": camera.status()}


@app.get("/api/status")
def status() -> dict:
    return {
        "camera": camera.status(),
        "faces": faces.stats(),
        "tts": tts.status(),
        "alarms": get_alarm_service().status(),
        "latest_results": latest_results,
    }


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
    saved = 0
    errors: list[str] = []

    for _ in range(req.samples):
        frame = camera.read()
        if frame is None:
            errors.append("No camera frame available")
            time.sleep(req.interval_ms / 1000)
            continue

        try:
            faces.register_sample(req.name, frame)
            saved += 1
        except ValueError as exc:
            errors.append(str(exc))
        except cv2.error as exc:
            errors.append(f"Face detection error: {exc}")

        time.sleep(req.interval_ms / 1000)

    if saved == 0:
        raise HTTPException(status_code=422, detail=errors[-1] if errors else "No samples saved")

    training = faces.train()
    return {
        "ok": True,
        "saved_samples": saved,
        "training": training,
        "last_error": errors[-1] if errors else "",
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

    annotated, _ = faces.annotate(frame)
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

            annotated, results = faces.annotate(frame)
            latest_results = results
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
    except (GeneratorExit, BrokenPipeError, ConnectionResetError):
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
        if result.get("recognized"):
            recognized_names.append(str(result.get("name", "")))
    primary = _primary_recognized_viewer(results)
    tts.update_face_state(recognized_names, primary_name=primary)
    if primary:
        _remember_voice_viewer(primary)


def _primary_recognized_viewer(results: list[dict]) -> str | None:
    """Largest recognized face in frame (closest to the camera)."""
    best_name: str | None = None
    best_area = 0
    for result in results:
        if not result.get("recognized"):
            continue
        name = str(result.get("name", "")).strip()
        if not name or name.lower() in {"unknown", "face"}:
            continue
        box = result.get("box") or {}
        area = int(box.get("w", 0)) * int(box.get("h", 0))
        if area > best_area:
            best_area = area
            best_name = name
    return best_name


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


def _camera_identity_snapshot() -> tuple[str | None, Literal["recognized", "unknown", "no_face"]]:
    """Live camera identity for 'who am I?' — recognized name or unknown / no face."""
    results: list[dict] = []
    frame = camera.read()
    if frame is not None:
        results = faces.recognize(frame)
    elif latest_results:
        results = latest_results

    if not results:
        return None, "no_face"

    primary = max(
        results,
        key=lambda r: int(r["box"]["w"]) * int(r["box"]["h"]),
    )
    if primary.get("recognized"):
        name = str(primary.get("name", "")).strip()
        if name and name.lower() not in {"unknown", "face"}:
            return name, "recognized"

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


async def _voice_ws_pipeline(websocket: WebSocket) -> None:
    """Whisper STT → Ollama → SAPI WAV. Multiple receive_bytes → send_bytes cycles per connection."""
    await websocket.accept()
    from voice_service import (
        SERVO_360_TRIGGER_DELAY_SECONDS,
        VoiceReplyMeta,
        minimal_voice_reply_wav,
        process_voice_wav,
    )

    session_viewer: str | None = None

    try:
        while True:
            try:
                wav_in = await websocket.receive_bytes()
            except WebSocketDisconnect:
                break

            wav_out: bytes | None = None
            reply_meta = VoiceReplyMeta()
            active_viewer: str | None = session_viewer
            try:
                viewer = await run_in_threadpool(_viewer_for_voice_query)
                if viewer:
                    session_viewer = viewer
                    active_viewer = viewer
                elif session_viewer:
                    active_viewer = session_viewer
                identity_name, identity_state = await run_in_threadpool(
                    _camera_identity_snapshot
                )

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
                err_note = str(exc)[:512]
                try:
                    from llm_service import brief_spoken_message
                    from wav_resample import ESP_PCM_SAMPLE_RATE_HZ, resample_wav_bytes_to_mono_16bit

                    def _recover_wav() -> bytes:
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

            if not wav_out:
                wav_out = minimal_voice_reply_wav()

            await run_in_threadpool(tts.notify_voice_interaction, active_viewer)

            try:
                await websocket.send_bytes(wav_out)
            except WebSocketDisconnect:
                break

            if reply_meta.trigger_servo_360:
                asyncio.create_task(
                    _delayed_esp_servo_360(SERVO_360_TRIGGER_DELAY_SECONDS)
                )
    finally:
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
        "--esp-play-wav-url",
        default=DEFAULT_ESP_PLAY_WAV_URL,
        help="POST synthesized WAV to this URL for ESP32-P4 speaker (e.g. http://IP/play_wav)",
    )
    parser.add_argument(
        "--ollama-url",
        default=os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434/api/generate"),
        help="Ollama generate API URL",
    )
    parser.add_argument(
        "--ollama-model",
        default=os.environ.get("OLLAMA_MODEL", "qwen2.5:1.5b"),
        help="Ollama model name (e.g. qwen2.5:1.5b)",
    )
    parser.add_argument(
        "--whisper-model",
        default=os.environ.get("WHISPER_MODEL", "tiny"),
        help="faster-whisper model for /ws/voice (tiny, base, small, ...)",
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
    args = parser.parse_args()

    camera_source = args.camera_url or args.camera_source
    os.environ["CAMERA_SOURCE"] = camera_source
    camera.source = camera_source

    if args.esp_play_wav_url.strip():
        os.environ["ESP_PLAY_WAV_URL"] = args.esp_play_wav_url.strip()

    if args.alarm_wav.strip():
        os.environ["ALARM_WAV_PATH"] = args.alarm_wav.strip()

    os.environ["OLLAMA_URL"] = args.ollama_url.strip()
    os.environ["OLLAMA_MODEL"] = args.ollama_model.strip()
    os.environ["WHISPER_MODEL"] = args.whisper_model.strip()

    if args.face_threshold is not None:
        os.environ["FACE_RECOGNITION_THRESHOLD"] = str(args.face_threshold)
    if args.face_margin is not None:
        os.environ["FACE_RECOGNITION_MARGIN"] = str(args.face_margin)
    if args.face_confirm_frames is not None:
        os.environ["FACE_CONFIRM_FRAMES"] = str(max(1, args.face_confirm_frames))

    tts.face_greeting_interval_seconds = max(1.0, float(args.face_greeting_interval))

    tts.configure_llm(
        ollama_url=args.ollama_url.strip(),
        ollama_model=args.ollama_model.strip(),
    )

    from voice_service import configure_from_environ

    configure_from_environ()
    import uvicorn

    try:
        uvicorn.run(app, host=args.host, port=args.port)
    except KeyboardInterrupt:
        # Graceful Ctrl+C without noisy traceback.
        pass


if __name__ == "__main__":
    main()
