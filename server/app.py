from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Literal

os.environ.setdefault("OPENCV_LOG_LEVEL", "SILENT")

import cv2
from fastapi import FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.staticfiles import StaticFiles as StarletteStaticFiles
from starlette.websockets import WebSocketState

from aux_recordings import list_aux_recordings, save_aux_wav, recordings_dir
from conversation_sessions import (
    begin_session as begin_voice_session,
    bind_session_user,
    list_recent_sessions,
    list_sessions_for_user,
    new_session_id,
)
from stream_asr import (
    UtteranceBuffer,
    looks_like_stream_pcm_frame,
    stream_idle_timeout_ends_session,
    stream_listen_max_ms,
)
from alarm_service import get_alarm_service
from camera import CameraPool, normalize_camera_rotation
from device_discovery import (
    discover_once as discover_devices_once,
    discovery_status,
    start_discovery_loop,
    stop_discovery_loop,
)
from device_registry import get_device_registry, resolve_device_id
from user_devices import normalize_device_mac
from eye_expression import normalize_eye_expression
from face_registration_service import capture_face_samples, configure_face_registration
from session_identity import configure_session_identity, get_session_identity
from face_service import FaceService
from memory_service import configure_from_environ as configure_memory_from_environ
from memory_service import get_memory_service, normalize_database_url
from esp_playback import (
    device_base_url,
    device_busy_speaking,
    ensure_esp_play_wav_url_configured,
    esp_play_wav_url,
    mark_device_busy_for,
)
from emotion_service import EmotionService
from object_detection_service import ObjectDetectionService, summarize_detections
from tts_service import (
    TTSService,
    preload_kokoro_voice,
    preload_piper_voice,
    synthesize_sapi_wav_bytes,
)
from vision_eye_driver import VisionEyeDriver
from pipeline_log import (
    UvicornPollFilter,
    begin_pipeline,
    end_pipeline,
    pipeline_log,
    uvicorn_log_config,
)
from weather_service import (
    DeviceLocationUnavailableError,
    WeatherUnavailableError,
    get_weather_service,
)

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
_voice_query_by_device: dict[str, int] = {}


def _voice_query_key(device_id: str | None) -> str:
    from user_devices import normalize_device_mac

    return normalize_device_mac(device_id) or str(device_id or "").strip() or "_"


def begin_voice_query(device_id: str | None = None) -> None:
    key = _voice_query_key(device_id)
    with _voice_query_lock:
        _voice_query_by_device[key] = _voice_query_by_device.get(key, 0) + 1


def end_voice_query(device_id: str | None = None) -> None:
    key = _voice_query_key(device_id)
    with _voice_query_lock:
        _voice_query_by_device[key] = max(0, _voice_query_by_device.get(key, 0) - 1)


def voice_pipeline_active(device_id: str | None = None) -> bool:
    with _voice_query_lock:
        if device_id:
            return _voice_query_by_device.get(_voice_query_key(device_id), 0) > 0
        return any(count > 0 for count in _voice_query_by_device.values())


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
    logging.getLogger("uvicorn.access").addFilter(UvicornPollFilter())


async def _serve_uvicorn(host: str, port: int) -> None:
    import uvicorn

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="info",
        log_config=uvicorn_log_config(),
        timeout_graceful_shutdown=3,
        ws_max_queue=256,
    )
    server = uvicorn.Server(config)
    logging.getLogger("uvicorn.access").addFilter(UvicornPollFilter())
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

registry = get_device_registry()
cameras = CameraPool()
faces = FaceService(BASE_DIR / "data")
# Face registration / UI enroll uses the selected UI device's camera.
face_registration = configure_face_registration(
    faces, cameras.frame_getter(registry.ui_device_id())
)
face_registration.set_device_id(registry.ui_device_id())
session_identity = configure_session_identity(
    faces,
    cameras.frame_getter(registry.ui_device_id()),
    cameras.frame_getter,
)
emotion = EmotionService()
objects = ObjectDetectionService()
_tts_face_interval = float(os.environ.get("FACE_GREETING_INTERVAL_SECONDS", "600"))
tts = TTSService(cooldown_seconds=0.0, face_greeting_interval_seconds=_tts_face_interval)
tts.set_playback_device_id(registry.ui_device_id())
# Camera emotion -> eyes (lowest priority; yields to voice queries + alarms/greetings).
vision_eye = VisionEyeDriver(
    voice_active_fn=voice_pipeline_active,
    speaking_fn=tts.is_speaking,
)
latest_results: list[dict] = []
_latest_results_by_device: dict[str, list[dict]] = {}
# Update vision-driven TTS every frame so greetings start as soon as recognition succeeds
# (throttling here added a noticeable delay before enqueue).
TTS_UPDATE_INTERVAL_SECONDS = 0.0

# Remember who was recognized for voice follow-ups (survives brief detection gaps).
_voice_viewer_lock = threading.Lock()
_voice_viewer_by_device: dict[str, tuple[str, float]] = {}
VOICE_VIEWER_TTL_SECONDS = float(os.environ.get("VOICE_VIEWER_TTL_SECONDS", "120"))


class CameraRequest(BaseModel):
    source: str = Field(..., min_length=1)
    device_id: str = ""


class CameraOrientationRequest(BaseModel):
    rotation: str = Field(..., min_length=1, max_length=32)
    device_id: str = ""


class RegisterRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    samples: int = Field(15, ge=1, le=80)
    interval_ms: int = Field(150, ge=50, le=2000)
    device_id: str = ""


class AlarmAckRequest(BaseModel):
    response: str = Field(..., min_length=1, max_length=32)


class DeviceSelectRequest(BaseModel):
    device_id: str = Field(..., min_length=1, max_length=64)


class DeviceLocationRequest(BaseModel):
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    location_name: str | None = Field(default=None, max_length=100)


class DeviceWifiNetworkRequest(BaseModel):
    ssid: str = Field(..., min_length=1, max_length=32)
    bssid: str = Field(..., min_length=17, max_length=17)
    rssi: int | None = Field(default=None, ge=-127, le=0)
    channel: int | None = Field(default=None, ge=1, le=196)


@app.on_event("startup")
def startup() -> None:
    logging.getLogger("uvicorn.access").addFilter(UvicornPollFilter())
    _load_env_file()
    faces.apply_settings_from_environ()
    face_registration.apply_settings_from_environ()
    configure_memory_from_environ()
    preload_piper_voice()
    preload_kokoro_voice()
    from voice_service import preload_whisper_model

    preload_whisper_model()
    get_memory_service().startup()
    get_alarm_service().start()
    registry.reload()
    # Start each server run from a fresh LAN inventory. This removes devices
    # persisted by a previous run when they no longer answer mDNS/UDP discovery.
    # An empty first scan keeps the persisted robots (mDNS is often late).
    discover_devices_once(replace_registry=True)
    cameras.start_all()
    # Discovery runs in its own daemon thread. Startup remains responsive when
    # no robots or multicast-capable network are present.
    start_discovery_loop(on_registry_updated=cameras.configure_from_registry)
    tts.set_playback_device_id(registry.ui_device_id())
    face_registration.set_device_id(registry.ui_device_id())
    face_registration.set_frame_getter(cameras.frame_getter(registry.ui_device_id()))
    ensure_esp_play_wav_url_configured()

    def _on_vision_greeting_spoken(
        person_name: str,
        spoken_text: str,
        reply_path: str,
        memory_store: str,
        continue_listen: bool,
    ) -> None:
        _append_latency_record(
            _latency_log_record(
                event="vision_greeting",
                reply_path=reply_path,
                heard="[face recognized]",
                reply_text=(spoken_text or "")[:200],
                voice_viewer=person_name,
                memory_viewer=person_name,
                memory_store=memory_store,
                continue_listen=continue_listen,
            )
        )

    tts.set_on_greeting_spoken(_on_vision_greeting_spoken)
    device_play = [
        f"{d.device_id}={d.effective_play_wav_url()}"
        for d in registry.list_devices()
        if d.effective_play_wav_url()
    ]
    play_url = esp_play_wav_url()
    if device_play:
        logger.info("ESP playback URLs: %s", ", ".join(device_play))
    elif play_url:
        logger.info("ESP speaker/eyes playback URL (legacy): %s", play_url)
    else:
        logger.warning(
            "ESP_PLAY_WAV_URL not set — face greeting TTS will not reach the board. "
            "Set devices.json or ESP_PLAY_WAV_URL / CAMERA_SOURCE=http://<ESP_IP>/stream"
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
    logger.info("Devices: %s", registry.status())
    import threading

    def _multi_device_vision_loop() -> None:
        """Emotion / TTS / result cache for every device (throttled).

        Identity greet + register live on the stream session (session_identity),
        not this MJPEG loop. Camera auto-welcome and auto-register are off.
        """
        interval = float(os.environ.get("MULTI_DEVICE_VISION_INTERVAL_S", "0.5"))
        while True:
            try:
                for record in registry.list_devices():
                    if not record.effective_camera_url():
                        continue
                    _vision_tick_device(
                        record.device_id,
                        update_tts=True,
                    )
            except Exception:
                logger.debug("multi-device vision tick failed", exc_info=True)
            time.sleep(max(0.2, interval))

    threading.Thread(
        target=_multi_device_vision_loop,
        daemon=True,
        name="multi-device-vision",
    ).start()

    # Loading YOLO weights takes a few seconds; keep it off the first camera frame.
    threading.Thread(
        target=objects.warmup,
        daemon=True,
        name="yolo-warmup",
    ).start()

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
        stop_discovery_loop()
    except BaseException:
        pass
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
        cameras.stop_all()
    except BaseException as exc:
        print(f"Camera shutdown warning: {exc}")


@app.get("/")
def index(request: Request):
    device_id = resolve_device_id(request.query_params.get("device_id"))
    rec = registry.get(device_id)
    camera_source = rec.effective_camera_url() if rec else ""
    response = templates.TemplateResponse(
        request,
        "index.html",
        {
            "camera_source": camera_source,
            "device_id": device_id,
            "devices": registry.status().get("devices", []),
        },
    )
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response


@app.post("/recordings")
async def upload_aux_recording(
    request: Request,
    device_id: str = Query(""),
    turn: int | None = Query(None),
    energy: int | None = Query(None),
    session: str = Query(""),
):
    """Store a Sirena Aux-in WAV from the P4 (also used when ASR is skipped)."""
    wav = await request.body()
    if not wav:
        raise HTTPException(status_code=400, detail="empty audio body")
    header_id = (request.headers.get("x-nino-device-id") or "").strip()
    resolved = resolve_device_id(device_id or header_id or None)
    try:
        path = await run_in_threadpool(
            lambda: save_aux_wav(
                wav,
                device_id=resolved,
                turn=turn,
                energy=energy,
                session=session,
                source="aux",
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    rel = path.relative_to(recordings_dir()).as_posix()
    logger.info(
        "Saved Aux-in recording %s (%d bytes) device=%s turn=%s energy=%s",
        rel,
        len(wav),
        resolved,
        turn,
        energy,
    )
    return {
        "ok": True,
        "path": rel,
        "bytes": len(wav),
        "url": f"/recordings/{rel}",
    }


@app.get("/recordings")
def recordings_index(limit: int = Query(50, ge=1, le=200)):
    return {"ok": True, "recordings": list_aux_recordings(limit=limit)}


@app.get("/recordings/{device_id}/{filename}")
def download_aux_recording(device_id: str, filename: str):
    if "/" in filename or "\\" in filename or filename.startswith("."):
        raise HTTPException(status_code=400, detail="invalid filename")
    path = recordings_dir() / device_id / filename
    try:
        path.resolve().relative_to(recordings_dir().resolve())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid path") from exc
    if not path.is_file() or path.suffix.lower() != ".wav":
        raise HTTPException(status_code=404, detail="recording not found")
    return FileResponse(path, media_type="audio/wav", filename=filename)


@app.get("/sessions")
def sessions_index(
    user: str = Query(""),
    limit: int = Query(50, ge=1, le=200),
):
    """Conversation session history, optionally filtered by user name."""
    if user.strip():
        return {"ok": True, "sessions": list_sessions_for_user(user, limit=limit)}
    return {"ok": True, "sessions": list_recent_sessions(limit=limit)}


@app.get("/actions")
def actions_page(request: Request):
    device_id = resolve_device_id(request.query_params.get("device_id"))
    response = templates.TemplateResponse(
        request,
        "actions.html",
        {
            "device_id": device_id,
            "devices": registry.status().get("devices", []),
        },
    )
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response


def _servo_bot_base(device_id: str | None) -> str:
    base = device_base_url(device_id)
    if not base:
        raise HTTPException(
            status_code=503,
            detail="No bot base URL (set devices.json or ESP_PLAY_WAV_URL)",
        )
    return base.rstrip("/")


def _servo_forward(method: str, path: str, device_id: str | None, payload: dict | None = None) -> dict:
    import requests

    base = _servo_bot_base(device_id)
    url = f"{base}{path}"
    try:
        if method == "GET":
            resp = requests.get(url, timeout=5.0)
        else:
            resp = requests.post(url, json=payload or {}, timeout=8.0)
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Bot unreachable: {exc}") from exc

    try:
        data = resp.json()
    except ValueError:
        data = {"ok": False, "error": "invalid_json", "raw": resp.text[:200]}
    if not resp.ok:
        detail = data.get("error") if isinstance(data, dict) else resp.text
        raise HTTPException(status_code=resp.status_code, detail=detail or "bot_error")
    if isinstance(data, dict):
        data["_base_url"] = base
    return data


@app.get("/api/servo/position")
def api_servo_position(device_id: str | None = None) -> dict:
    return _servo_forward("GET", "/servo/position", resolve_device_id(device_id))


@app.post("/api/servo/record")
async def api_servo_record(request: Request, device_id: str | None = None) -> dict:
    body = await request.json()
    return _servo_forward("POST", "/servo/record", resolve_device_id(device_id), body)


@app.post("/api/servo/goal")
async def api_servo_goal(request: Request, device_id: str | None = None) -> dict:
    body = await request.json()
    return _servo_forward("POST", "/servo/goal", resolve_device_id(device_id), body)


@app.post("/api/servo/play")
async def api_servo_play(request: Request, device_id: str | None = None) -> dict:
    body = await request.json()
    return _servo_forward("POST", "/servo/play", resolve_device_id(device_id), body)


@app.post("/api/camera")
def set_camera(req: CameraRequest) -> dict:
    device_id = resolve_device_id(req.device_id or None)
    stream = cameras.restart(device_id, req.source)
    return {"ok": True, "device_id": device_id, "camera": stream.status()}


@app.post("/api/camera/orientation")
def set_camera_orientation(req: CameraOrientationRequest) -> dict:
    device_id = resolve_device_id(req.device_id or None)
    rotation = normalize_camera_rotation(req.rotation)
    if rotation is None:
        raise HTTPException(
            status_code=422,
            detail="Rotation must be none, cw90, 180, or ccw90",
        )
    try:
        record = registry.set_camera_rotation(device_id, rotation)
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown device_id: {exc.args[0]!r}",
        ) from exc
    device_id = record.device_id
    stream = cameras.set_rotation(device_id, rotation)
    logger.info("Camera orientation set device_id=%s rotation=%s", device_id, rotation)
    return {
        "ok": True,
        "device_id": device_id,
        "rotation": rotation,
        "camera": stream.status(),
    }


@app.post("/api/device")
def select_device(req: DeviceSelectRequest) -> dict:
    device_id = registry.set_ui_device_id(req.device_id)
    tts.set_playback_device_id(device_id)
    face_registration.set_device_id(device_id)
    face_registration.set_frame_getter(cameras.frame_getter(device_id))
    ident = get_session_identity(device_id)
    if ident is not None:
        ident.set_frame_getter(cameras.frame_getter(device_id))
    return {"ok": True, "devices": registry.status()}


@app.get("/api/devices")
def list_devices() -> dict:
    return {"ok": True, **registry.status(), "discovery": discovery_status()}


@app.post("/api/devices/discover")
def discover_devices() -> dict:
    """Force a LAN-only mDNS/UDP discovery scan."""
    devices = discover_devices_once()
    return {
        "ok": True,
        "found": [device.device_id for device in devices],
        **registry.status(),
        "discovery": discovery_status(),
    }


@app.post("/api/devices/{device_id}/location")
def update_device_location(
    device_id: str,
    req: DeviceLocationRequest,
    x_nino_location_token: str | None = Header(default=None),
) -> dict:
    """Record the latest GPS/location fix reported by a registered device."""
    expected_token = os.environ.get("NINO_LOCATION_TOKEN", "").strip()
    if expected_token and not hmac.compare_digest(
        x_nino_location_token or "", expected_token
    ):
        raise HTTPException(status_code=401, detail="Invalid device location token")
    if registry.get(device_id) is None:
        raise HTTPException(status_code=404, detail="Unknown device_id")
    try:
        device = registry.set_location(
            device_id,
            latitude=req.latitude,
            longitude=req.longitude,
            location_name=req.location_name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "ok": True,
        "device_id": device.device_id,
        "location": {
            "latitude": device.latitude,
            "longitude": device.longitude,
            "name": device.location_name,
            "updated_at": device.location_updated_at,
        },
    }


@app.post("/api/devices/{device_id}/network")
def update_device_wifi_network(
    device_id: str,
    req: DeviceWifiNetworkRequest,
    request: Request,
) -> dict:
    """Record Wi-Fi identity reported after a registered device connects."""
    mac = normalize_device_mac(device_id)
    host = str(getattr(request.client, "host", "") or "") if request.client else ""
    created = False
    if not mac:
        mac, created = registry.ensure_from_client_host(host)
    if not mac or registry.get(mac) is None:
        raise HTTPException(status_code=404, detail="Unknown device_id")
    if created:
        cameras.configure_from_registry()
    try:
        device = registry.set_wifi_network(
            mac,
            ssid=req.ssid,
            bssid=req.bssid,
            rssi=req.rssi,
            channel=req.channel,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "ok": True,
        "device_id": device.device_id,
        "wifi": {
            "ssid": device.wifi_ssid,
            "bssid": device.wifi_bssid,
            "rssi": device.wifi_rssi,
            "channel": device.wifi_channel,
            "updated_at": device.wifi_updated_at,
        },
    }


@app.get("/api/weather")
def current_weather(device_id: str | None = None) -> dict:
    """Return cached current conditions for the named device's location."""
    device = registry.resolve_or_default(device_id)
    try:
        weather = get_weather_service().current_for_device(device)
    except DeviceLocationUnavailableError as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                "No location is configured for this device. "
                "POST its latitude and longitude to /api/devices/{device_id}/location."
            ),
        ) from exc
    except WeatherUnavailableError as exc:
        raise HTTPException(
            status_code=503, detail="Current weather is temporarily unavailable"
        ) from exc
    return {
        "ok": True,
        "device_id": device.device_id,
        "location": {
            "latitude": device.latitude,
            "longitude": device.longitude,
            "name": device.location_name,
            "updated_at": device.location_updated_at,
        },
        "weather": weather,
    }


class MusicPlayRequest(BaseModel):
    query: str = Field(min_length=1, max_length=200)
    device_id: str | None = None
    # False arms the stream without calling the robot, for PC-side testing
    # before the /music firmware lands.
    push_to_device: bool = True


@app.get("/music/stream.wav")
def music_stream(device_id: str | None = None) -> StreamingResponse:
    """Continuous mono 16-bit PCM the robot pulls while a track is playing."""
    from music_service import MusicNoDeviceError, get_music_service

    service = get_music_service()
    try:
        chunks = service.iter_stream(device_id)
    except MusicNoDeviceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return StreamingResponse(
        chunks,
        media_type="audio/wav",
        headers={"Cache-Control": "no-store", "Accept-Ranges": "none"},
    )


@app.post("/api/music/play")
def music_play(req: MusicPlayRequest) -> dict:
    from music_service import MusicNoDeviceError, get_music_service
    from music_source import (
        MusicNotConfiguredError,
        MusicNotFoundError,
        MusicUnavailableError,
    )

    service = get_music_service()
    try:
        track = service.play(
            req.device_id, req.query, notify_device=req.push_to_device
        )
    except MusicNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except MusicNotConfiguredError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    except MusicNoDeviceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except MusicUnavailableError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "ok": True,
        "track": {
            "title": track.title,
            "artist": track.artist,
            "duration_seconds": track.duration_seconds,
            "page_url": track.page_url,
        },
        "stream_url": service.stream_url_for(req.device_id),
    }


@app.post("/api/music/stop")
def music_stop(device_id: str | None = None) -> dict:
    from music_service import get_music_service

    return {"ok": True, "was_playing": get_music_service().stop(device_id)}


@app.get("/api/music/status")
def music_status(device_id: str | None = None) -> dict:
    from music_service import get_music_service
    from music_source import ffmpeg_path

    try:
        import yt_dlp  # noqa: F401

        ytdlp_ready = True
    except ImportError:
        ytdlp_ready = False

    return {
        "ok": True,
        "ffmpeg": bool(ffmpeg_path()),
        "yt_dlp": ytdlp_ready,
        **get_music_service().status(device_id),
    }


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


def _stt_status() -> dict:
    from voice_service import whisper_runtime_status

    return whisper_runtime_status()


@app.get("/api/status")
def status(device_id: str | None = None) -> dict:
    from llm_service import ollama_runtime_status

    active = resolve_device_id(device_id)
    return {
        "device_id": active,
        "devices": registry.status(),
        "discovery": discovery_status(),
        "camera": cameras.status(active),
        "cameras": cameras.status(),
        "faces": faces.stats(),
        "emotion": emotion.stats(),
        "objects": objects.stats(),
        "voice_pipeline_active": voice_pipeline_active(),
        "tts": tts.status(),
        "stt": _stt_status(),
        "llm": ollama_runtime_status(
            model=os.environ.get("OLLAMA_MODEL"),
            api_url=os.environ.get("OLLAMA_URL"),
        ),
        "alarms": get_alarm_service().status(),
        "memory": get_memory_service().status(),
        "face_registration": face_registration.status(),
        "latest_results": _cached_face_results(active) or latest_results,
    }


@app.get("/api/objects")
def detected_objects(device_id: str | None = None, refresh: bool = False) -> dict:
    """Objects YOLO26 currently sees for a device."""
    active = resolve_device_id(device_id)
    if refresh:
        frame = cameras.read(active)
        detections = _detect_objects(frame, active) if frame is not None else []
    else:
        detections = objects.latest(active)
    return {
        "ok": True,
        "device_id": active,
        "objects": detections,
        "summary": summarize_detections(detections),
        "detector": objects.stats(),
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
    device_id = resolve_device_id(req.device_id or None)
    read_frame = cameras.frame_getter(device_id)
    probe = read_frame()
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
        read_frame,
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
        "device_id": device_id,
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


def _detect_objects(frame, device_id: str, *, force: bool = False) -> list[dict]:
    """YOLO26 detections for a frame; never let object detection break the feed."""
    try:
        return objects.detect(frame, device_id=device_id, force=force)
    except Exception:
        logger.debug("Object detection failed for %s", device_id, exc_info=True)
        return []


@app.get("/snapshot.jpg")
def snapshot(device_id: str | None = None) -> Response:
    active = resolve_device_id(device_id)
    frame = cameras.read(active)
    if frame is None:
        raise HTTPException(status_code=503, detail="No camera frame available")

    results = faces.recognize(frame, device_id=active)
    try:
        emotion.attach_emotions(frame, results)
    except Exception:
        logger.exception("Emotion detection failed for snapshot")
    detections = _detect_objects(frame, active)
    annotated, _ = faces.annotate(frame, results=results)
    objects.annotate(annotated, detections)
    ok, encoded = cv2.imencode(".jpg", annotated)
    if not ok:
        raise HTTPException(status_code=500, detail="Could not encode snapshot")

    return Response(content=encoded.tobytes(), media_type="image/jpeg")


@app.get("/video_feed")
def video_feed(device_id: str | None = None) -> StreamingResponse:
    active = resolve_device_id(device_id)
    return StreamingResponse(
        _mjpeg_generator(active),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


def _mjpeg_generator(device_id: str):
    global latest_results
    last_tts_update_at = 0.0
    is_ui_device = device_id == registry.ui_device_id()
    try:
        while True:
            frame = cameras.read(device_id)
            if frame is None:
                if is_ui_device:
                    tts.update_face_state([], device_id=device_id)
                time.sleep(0.02)
                continue

            results = faces.recognize(frame, device_id=device_id)
            try:
                emotion.attach_emotions(frame, results)
            except Exception:
                logger.exception("Emotion detection failed; continuing MJPEG")
            detections = _detect_objects(frame, device_id)
            annotated, results = faces.annotate(frame, results=results)
            objects.annotate(annotated, detections)
            key = _results_device_key(device_id)
            if key:
                _latest_results_by_device[key] = results
            if is_ui_device:
                latest_results = results
            now = time.time()
            if now - last_tts_update_at >= TTS_UPDATE_INTERVAL_SECONDS:
                _update_tts_face_state(results, device_id=device_id)
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


def _vision_tick_device(
    device_id: str, *, update_tts: bool = True
) -> None:
    """One recognition/emotion pass for a device (used by the background vision loop)."""
    frame = cameras.read(device_id)
    if frame is None:
        vision_eye.update([], device_id=device_id)
        return
    results = faces.recognize(frame, device_id=device_id)
    try:
        emotion.attach_emotions(frame, results)
    except Exception:
        logger.debug("Emotion tick failed for %s", device_id, exc_info=True)
    # Keeps the object cache warm for voice queries with no browser attached.
    _detect_objects(frame, device_id)
    key = _results_device_key(device_id)
    if key:
        _latest_results_by_device[key] = results
    vision_eye.update(results, device_id=device_id)
    if update_tts:
        _update_tts_face_state(results, device_id=device_id)


def _remember_voice_viewer(name: str | None, device_id: str | None = None) -> None:
    if not name:
        return
    cleaned = str(name).strip()
    if not cleaned or cleaned.lower() in {"unknown", "face"}:
        return
    from user_devices import normalize_device_mac

    key = normalize_device_mac(device_id)
    if not key:
        return
    with _voice_viewer_lock:
        _voice_viewer_by_device[key] = (cleaned, time.time())


def _forget_voice_viewer(device_id: str | None = None) -> None:
    from user_devices import normalize_device_mac

    key = normalize_device_mac(device_id)
    if not key:
        return
    with _voice_viewer_lock:
        _voice_viewer_by_device.pop(key, None)


def _recall_voice_viewer(device_id: str | None = None) -> str | None:
    from user_devices import normalize_device_mac

    key = normalize_device_mac(device_id)
    if not key:
        return None
    with _voice_viewer_lock:
        entry = _voice_viewer_by_device.get(key)
        if not entry:
            return None
        name, seen_at = entry
        if time.time() - seen_at > VOICE_VIEWER_TTL_SECONDS:
            return None
        return name


def _results_device_key(device_id: str | None) -> str:
    from user_devices import normalize_device_mac

    return normalize_device_mac(device_id) or str(device_id or "").strip()


def _update_tts_face_state(
    results: list[dict], *, device_id: str | None = None
) -> None:
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
    tts.update_face_state(recognized_names, primary_name=primary, device_id=device_id)
    if primary:
        _remember_voice_viewer(primary, device_id)


def _cached_face_results(device_id: str | None) -> list[dict]:
    key = _results_device_key(device_id)
    if not key:
        return []
    return list(_latest_results_by_device.get(key) or [])


def _primary_recognized_viewer(
    results: list[dict], *, allow_pending: bool = False
) -> str | None:
    """Largest primary face in frame with a confident identity."""
    return faces.primary_viewer(results, allow_pending=allow_pending)


def _recognized_viewer_names(
    results: list[dict], *, allow_pending: bool = False
) -> list[str]:
    """All confident identities currently in the overlay cache, largest first."""
    ranked: list[tuple[int, str]] = []
    seen: set[str] = set()
    for result in results or []:
        name = ""
        if result.get("recognized") or result.get("stabilized"):
            name = str(result.get("name", "")).strip()
        elif allow_pending and result.get("pending"):
            name = str(result.get("candidate_name") or result.get("name") or "").strip()
        key = name.lower()
        if not name or key in {"unknown", "face"} or key in seen:
            continue
        box = result.get("box") or {}
        area = int(box.get("w", 0) or 0) * int(box.get("h", 0) or 0)
        seen.add(key)
        ranked.append((area, name))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [name for _area, name in ranked]


def _viewer_for_voice_query(device_id: str | None = None) -> str | None:
    """Who is speaking — overlay cache first, then live frame, then session memory."""
    active = resolve_device_id(device_id)
    name: str | None = None

    cached = _cached_face_results(active)
    if cached:
        name = _primary_recognized_viewer(cached, allow_pending=True)

    if not name:
        frame = cameras.read(active)
        if frame is not None:
            name = _primary_recognized_viewer(
                faces.recognize(frame, device_id=active), allow_pending=True
            )

    if not name:
        name = _recall_voice_viewer(active)

    if name:
        _remember_voice_viewer(name, active)

    return name


def _camera_identity_snapshot(
    require_live_face: bool = False,
    device_id: str | None = None,
) -> tuple[str | None, Literal["recognized", "unknown", "no_face"]]:
    """Live camera identity for voice queries and 'who am I?' prompts."""
    active = resolve_device_id(device_id)
    cached = _cached_face_results(active)
    # Prefer the same live detection stream shown in UI so voice identity follows
    # what the user sees in the camera overlay.
    if cached:
        primary = _primary_recognized_viewer(cached, allow_pending=True)
        if primary:
            _remember_voice_viewer(primary, active)
            return primary, "recognized"
        if require_live_face:
            has_primary_face = any(r.get("primary", True) for r in cached)
            if has_primary_face:
                return None, "unknown"

    # Fresh frame fallback (keeps identity responsive even if latest_results lags).
    frame = cameras.read(active)
    if frame is not None:
        results = faces.recognize(frame, device_id=active)
        primary = _primary_recognized_viewer(results)
        if primary:
            _remember_voice_viewer(primary, active)
            return primary, "recognized"
        if require_live_face and results:
            return None, "unknown"

    # Multi-frame vote fallback.
    name, state = faces.recognize_identity(
        cameras.frame_getter(active),
        allow_session_hint=not require_live_face,
        device_id=active,
    )
    if state == "recognized" and name:
        _remember_voice_viewer(name, active)
        return name, "recognized"
    if state == "no_face":
        if require_live_face:
            return None, "no_face"
        recalled = _recall_voice_viewer(active)
        if recalled:
            return recalled, "recognized"
        return None, "no_face"
    return None, "unknown"


def _camera_scene_snapshot(device_id: str | None = None) -> str:
    """What the camera can see right now, phrased for the LLM prompt."""
    if not objects.enabled:
        return ""
    active = resolve_device_id(device_id)
    detections = objects.latest(active)
    if not detections:
        # No background tick yet (or nothing seen last pass) — try a live frame.
        frame = cameras.read(active)
        if frame is not None:
            detections = _detect_objects(frame, active)
    return summarize_detections(detections)


def _live_visible_scene(
    device_id: str | None = None,
    *,
    force_objects: bool = False,
) -> tuple[list[str], list[dict]]:
    """People (overlay, then live frame) + objects. No stale session-hint names."""
    active = resolve_device_id(device_id)
    names: list[str] = []
    cached = _cached_face_results(active)
    if cached:
        names = _recognized_viewer_names(cached, allow_pending=True)
    frame = cameras.read(active)
    if not names and frame is not None:
        try:
            names = _recognized_viewer_names(
                faces.recognize(frame, device_id=active), allow_pending=True
            )
        except Exception:
            logger.debug("live scene face recognize failed", exc_info=True)
    detections: list[dict] = []
    if objects.enabled:
        if frame is not None:
            detections = _detect_objects(frame, active, force=force_objects)
        else:
            detections = objects.latest(active)
    return names, detections


from voice_service import configure_visible_scene_snapshot

configure_visible_scene_snapshot(
    lambda device_id: _live_visible_scene(device_id, force_objects=True)
)


def _session_open_identity_snapshot(
    device_id: str | None = None,
) -> tuple[str | None, Literal["recognized", "unknown", "no_face"]]:
    """Live identity for stream GREET after face hunt.

    Use the same recognition cache the browser overlay writes. Do not greet from
    a previous session, and do not call recognize() first — a dark/empty frame
    right after the robot camera powers on would wipe the stabilizer.
    """
    active = resolve_device_id(device_id)
    deadline = time.monotonic() + 2.5
    last_state: Literal["recognized", "unknown", "no_face"] = "no_face"
    while True:
        cached = _cached_face_results(active)
        if cached:
            primary = _primary_recognized_viewer(cached, allow_pending=True)
            if primary:
                _remember_voice_viewer(primary, active)
                logger.info(
                    "session-open identity cache name=%s device=%s",
                    primary,
                    active,
                )
                return primary, "recognized"
            if any(r.get("detection_valid") or r.get("box") for r in cached):
                last_state = "unknown"
        if time.monotonic() >= deadline:
            break
        time.sleep(0.2)

    name, state = faces.recognize_identity(
        cameras.frame_getter(active),
        allow_session_hint=False,
        allow_pending=True,
        device_id=active,
    )
    if state == "recognized" and name:
        cleaned = str(name).strip()
        if cleaned and cleaned.lower() not in {"unknown", "face"}:
            _remember_voice_viewer(cleaned, active)
            logger.info(
                "session-open identity vote name=%s device=%s", cleaned, active
            )
            return cleaned, "recognized"
    recalled = _recall_voice_viewer(active)
    if recalled:
        logger.info(
            "session-open identity hunt-memory name=%s device=%s", recalled, active
        )
        return recalled, "recognized"
    logger.info("session-open identity state=%s device=%s", state or last_state, active)
    if state == "unknown" or last_state == "unknown":
        return None, "unknown"
    return None, "no_face"


def _live_scene_identity(
    device_id: str | None = None,
    *,
    wait_s: float = 0.8,
) -> tuple[list[str], Literal["recognized", "unknown", "no_face"]]:
    """Who is in frame right now after a hunt (for mid-session identity refresh)."""
    active = resolve_device_id(device_id)
    deadline = time.monotonic() + max(0.0, wait_s)
    last_state: Literal["recognized", "unknown", "no_face"] = "no_face"
    last_names: list[str] = []
    while True:
        cached = _cached_face_results(active)
        if cached:
            names = _recognized_viewer_names(cached, allow_pending=True)
            if names:
                _remember_voice_viewer(names[0], active)
                return names, "recognized"
            if any(r.get("detection_valid") or r.get("box") for r in cached):
                last_state = "unknown"
                last_names = []
        if time.monotonic() >= deadline:
            break
        time.sleep(0.15)
    recalled = _recall_voice_viewer(active)
    if last_state == "no_face" and recalled:
        return [recalled], "recognized"
    return last_names, last_state


async def _delayed_esp_servo_360(
    delay_seconds: float, device_id: str | None = None
) -> None:
    from voice_service import SERVO_360_TRIGGER_DELAY_SECONDS, trigger_esp_servo_360

    wait = delay_seconds if delay_seconds > 0 else SERVO_360_TRIGGER_DELAY_SECONDS
    await asyncio.sleep(wait)
    ok, err = await run_in_threadpool(trigger_esp_servo_360, device_id)
    if ok:
        logger.info("ESP servo 360 started after voice confirmation device=%s", device_id)
    else:
        logger.warning(
            "ESP servo 360 failed after voice confirmation device=%s: %s",
            device_id,
            err or "unknown",
        )


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


def _ws_is_connected(websocket: WebSocket) -> bool:
    return (
        websocket.client_state == WebSocketState.CONNECTED
        and websocket.application_state == WebSocketState.CONNECTED
    )


async def _ws_send_json(websocket: WebSocket, data: dict[str, object]) -> bool:
    """Send JSON. False if the P4 already closed the socket (do not crash ASGI)."""
    if not _ws_is_connected(websocket):
        return False
    try:
        await websocket.send_json(data)
        return True
    except (WebSocketDisconnect, RuntimeError) as exc:
        logger.info("WS send_json skipped: %s", exc)
        return False


async def _ws_send_bytes(websocket: WebSocket, data: bytes) -> bool:
    """Send binary. False if the P4 already closed the socket (do not crash ASGI)."""
    if not _ws_is_connected(websocket):
        return False
    try:
        await websocket.send_bytes(data)
        return True
    except (WebSocketDisconnect, RuntimeError) as exc:
        logger.info("WS send_bytes skipped: %s", exc)
        return False


def _session_kind_from_websocket(websocket: WebSocket) -> str:
    raw = (websocket.query_params.get("session") or "wake").strip().lower()
    if raw in {"continue", "conv", "followup", "ack"}:
        return "continue"
    return "wake"


def _aux_energy_from_websocket(websocket: WebSocket) -> int | None:
    raw = (websocket.query_params.get("energy") or "").strip()
    if not raw:
        return None
    try:
        return max(0, int(raw))
    except ValueError:
        return None


def _voice_turn_from_websocket(websocket: WebSocket) -> int | None:
    raw = (websocket.query_params.get("turn") or "").strip()
    if not raw:
        return None
    try:
        return max(0, int(raw))
    except ValueError:
        return None


def _session_id_from_websocket(websocket: WebSocket) -> str:
    raw = (websocket.query_params.get("session_id") or "").strip()
    return raw or new_session_id()


def _voice_wants_stream(websocket: WebSocket) -> bool:
    raw = (websocket.query_params.get("stream") or "").strip().lower()
    return raw in {"1", "true", "yes", "pcm"}


def _device_id_from_websocket(websocket: WebSocket) -> str:
    """Resolve the robot MAC from query param, header, or client IP.

    All-zero and name-based device_id values are not treated as MACs. In that
    case the voice socket is mapped to a discovered robot by exact client IP.
    """
    raw = (websocket.query_params.get("device_id") or "").strip()
    if not raw:
        raw = (websocket.headers.get("x-nino-device-id") or "").strip()
    mac = normalize_device_mac(raw)
    if mac:
        return resolve_device_id(mac)
    host = ""
    if websocket.client is not None:
        host = str(getattr(websocket.client, "host", "") or "")
    mapped = get_device_registry().find_device_id_by_host(host)
    created = False
    if not mapped:
        mapped, created = get_device_registry().ensure_from_client_host(host)
        if created:
            cameras.configure_from_registry()
    if mapped:
        logger.info(
            "Voice WS mapped client %s raw=%r -> %s",
            _ws_client_label(websocket),
            raw,
            mapped,
        )
        return mapped
    logger.warning(
        "Voice WS missing or non-MAC device_id from %s raw=%r",
        _ws_client_label(websocket),
        raw,
    )
    return ""


async def _process_voice_query_audio(
    wav_in: bytes,
    *,
    device_id: str,
    session_kind: str,
    session_id: str,
    aux_energy: int | None,
    voice_turn: int | None,
    client_label: str,
) -> tuple[bytes | None, object, str | None]:
    """Run STT → LLM → TTS. Returns (wav_out, reply_meta, pipeline_error)."""
    from voice_service import (
        VoiceReplyMeta,
        minimal_voice_reply_wav,
        process_voice_wav,
    )

    wav_out: bytes | None = None
    reply_meta = VoiceReplyMeta(device_id=device_id, session_id=session_id)
    pipeline_error: str | None = None
    t_query_start = time.perf_counter()
    identity_seconds = 0.0
    await run_in_threadpool(begin_voice_query, device_id)
    try:
        try:
            t_ident = time.perf_counter()
            identity_name, identity_state = await run_in_threadpool(
                _camera_identity_snapshot, True, device_id
            )
            active_viewer: str | None = None
            if identity_state == "recognized" and identity_name:
                normalized_identity = str(identity_name).strip()
                if normalized_identity and normalized_identity.lower() not in {
                    "unknown",
                    "face",
                }:
                    active_viewer = normalized_identity
            if not active_viewer:
                active_viewer = await run_in_threadpool(
                    _recall_voice_viewer, device_id
                )
            identity_seconds = round(time.perf_counter() - t_ident, 3)
            camera_scene = await run_in_threadpool(
                _camera_scene_snapshot, device_id
            )
            visible_names = _recognized_viewer_names(
                _cached_face_results(device_id), allow_pending=True
            )

            def _run_voice() -> tuple[bytes, VoiceReplyMeta]:
                return process_voice_wav(
                    wav_in,
                    active_viewer,
                    camera_identity_name=identity_name,
                    camera_identity_state=identity_state,
                    camera_scene=camera_scene,
                    visible_names=visible_names,
                    device_id=device_id,
                    session_kind=session_kind,
                    session_id=session_id,
                    aux_energy=aux_energy,
                    voice_turn=voice_turn,
                )

            wav_out, reply_meta = await run_in_threadpool(_run_voice)
            if active_viewer and reply_meta.registered_face_name is None:
                reply_meta.timings.setdefault("voice_viewer", active_viewer)
        except Exception as exc:
            logger.exception("Voice pipeline failed device=%s", device_id)
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
        timings.setdefault("session", session_kind)
        timings.setdefault("session_id", session_id)
        server_total = round(time.perf_counter() - t_query_start, 3)
        process_total = float(timings.get("process_total_seconds") or 0)
        timings["identity_seconds"] = identity_seconds
        timings["server_total_seconds"] = server_total
        timings["overhead_seconds"] = round(max(0.0, server_total - process_total), 3)
        from voice_service import log_nino_voice

        log_nino_voice(
            "DONE",
            turn=timings.get("turn", voice_turn),
            device=device_id,
            session=session_kind,
            session_id=session_id,
            path=timings.get("reply_path"),
            wake_ok=timings.get("wake_ok"),
            continue_listen=int(bool(reply_meta.prompt_medical_ack)),
            end_session=int(bool(reply_meta.end_session)),
            heard=str(timings.get("heard") or "")[:120] or "(empty)",
            reply=str(timings.get("reply_text") or "")[:120],
            stt=float(timings.get("stt_seconds") or 0),
            llm=float(timings.get("reply_seconds") or 0),
            tts=float(timings.get("tts_seconds") or 0),
            process=process_total,
            identity=identity_seconds,
            server=server_total,
            in_s=float(timings.get("audio_in_seconds") or 0),
            out_s=float(timings.get("audio_out_seconds") or 0),
        )
        record = _latency_log_record(
            event="voice_query",
            client=client_label,
            device_id=device_id,
            **timings,
        )
        if pipeline_error is not None:
            record["error"] = pipeline_error
        await run_in_threadpool(_append_latency_record, record)
        return wav_out, reply_meta, pipeline_error
    finally:
        await run_in_threadpool(end_voice_query, device_id)


async def _voice_ws_send_reply(
    websocket: WebSocket,
    *,
    device_id: str,
    session_id: str,
    wav_out: bytes | None,
    reply_meta: object,
    client_label: str,
) -> bool:
    from voice_service import VoiceReplyMeta

    meta = reply_meta if isinstance(reply_meta, VoiceReplyMeta) else VoiceReplyMeta()
    ws_meta: dict[str, object] = {
        "type": "reply",
        "prompt_medical_ack": meta.prompt_medical_ack,
        "end_session": bool(meta.end_session),
        "continue_listen": bool(meta.prompt_medical_ack) and not meta.end_session,
        "device_id": device_id,
        "session_id": session_id or meta.session_id,
        "skip": False,
    }
    eye_tag = normalize_eye_expression(meta.eye_expression)
    if meta.timings.get("session_open") and eye_tag != "heart":
        # Hunt / register prompt: do not send curious/tired/etc. on session-open.
        eye_tag = None
    if eye_tag:
        ws_meta["eye_expression"] = eye_tag
        logger.info(
            "WS send eye_expression=%s device=%s client=%s",
            eye_tag,
            device_id,
            client_label,
        )
    motion = getattr(meta, "motion", None) or meta.timings.get("motion")
    if meta.timings.get("session_open") and list(motion or []) == ["curious"]:
        motion = None
    if motion:
        ws_meta["motion"] = list(motion)
        ws_meta["type"] = "reply"
    if getattr(meta, "look_scan", False):
        ws_meta["look_scan"] = True
    if getattr(meta, "face_track", None) is not None:
        ws_meta["face_track"] = bool(meta.face_track)
    if not await _ws_send_json(websocket, ws_meta):
        logger.info(
            "WS reply skipped, socket closed device=%s client=%s",
            device_id,
            client_label,
        )
        return False
    if wav_out and not await _ws_send_bytes(websocket, wav_out):
        logger.info(
            "WS WAV skipped, socket closed device=%s client=%s",
            device_id,
            client_label,
        )
        return False

    # Vaseekaran (d4) queues the WS WAV (wait_idle matches clip length) but the
    # ES8311 speaker stays silent. POST /play_wav after the board has reopened
    # Aux so the HTTP job forces a DAC reopen. Skip tiny wake-reject clips.
    # Do not do this on Gitam — that unit already plays the WS binary.
    if (
        wav_out
        and len(wav_out) > 8000
        and device_id == "b0a6048addd4"
    ):
        wav_copy = bytes(wav_out)
        eye_copy = eye_tag
        delay_s = max(0.5, (len(wav_copy) - 44) / 32000.0 + 0.45)
        play_s = max(0.5, (len(wav_copy) - 44) / 32000.0)
        mark_device_busy_for(delay_s + play_s + 0.8, device_id=device_id)
        logger.info(
            "HTTP play_wav fallback in %.2fs device=%s bytes=%s",
            delay_s,
            device_id,
            len(wav_copy),
        )

        async def _http_play_fallback() -> None:
            await asyncio.sleep(delay_s)
            def _http_play() -> None:
                try:
                    from esp_playback import deliver_wav_to_device

                    deliver_wav_to_device(
                        device_id,
                        wav_copy,
                        timeout=20.0,
                        prompt_ack=False,
                        prompt_ack_chime=False,
                        eye_expression=eye_copy,
                    )
                except Exception:
                    logger.exception(
                        "HTTP play_wav failed device=%s bytes=%s",
                        device_id,
                        len(wav_copy),
                    )

            await run_in_threadpool(_http_play)

        asyncio.create_task(_http_play_fallback())

    if meta.registered_face_name:
        _remember_voice_viewer(meta.registered_face_name, device_id)
    viewer = str(meta.timings.get("voice_viewer") or "")
    await run_in_threadpool(tts.notify_voice_interaction, viewer or None, device_id)

    from voice_listen_state import (
        mark_session_closed,
        mark_session_open,
        mark_tts_playback,
        post_tts_grace_seconds,
    )

    if meta.end_session:
        mark_session_closed(session_id, device_id)
    elif bool(meta.prompt_medical_ack):
        mark_session_open(session_id, device_id)

    if wav_out and len(wav_out) > 44:
        play_s = float(meta.timings.get("audio_out_seconds") or 0.0)
        if play_s <= 0.0:
            from esp_playback import _wav_duration_seconds

            play_s = _wav_duration_seconds(wav_out)
        mark_tts_playback(
            session_id,
            device_id,
            tts_seconds=float(meta.timings.get("tts_seconds") or 0.0),
            audio_out_seconds=play_s,
        )
        mark_device_busy_for(play_s + post_tts_grace_seconds(), device_id=device_id)

    from voice_service import SERVO_360_TRIGGER_DELAY_SECONDS

    if meta.trigger_servo_360:
        asyncio.create_task(
            _delayed_esp_servo_360(SERVO_360_TRIGGER_DELAY_SECONDS, device_id)
        )
    return True


async def _voice_ws_stream_pipeline(
    websocket: WebSocket,
    device_id: str,
    *,
    already_accepted: bool = False,
    first_chunk: bytes | None = None,
) -> None:
    """Stream Aux-in PCM until ASR end-of-speech, then TTS; loop until goodbye.

    Only this task calls websocket.receive() / send_*. Starlette shares one lock
    between send and receive; a background drain task stalls during STT/TTS,
    the P4 TCP window fills, and esp_transport_write returns 0.
    """
    if not already_accepted:
        await websocket.accept()
    from voice_service import log_nino_voice
    from wav_resample import is_wav_bytes, pcm16_mono_to_wav

    session_id = _session_id_from_websocket(websocket)
    aux_energy = _aux_energy_from_websocket(websocket)
    voice_turn = _voice_turn_from_websocket(websocket) or 0
    client_label = _ws_client_label(websocket)
    _forget_voice_viewer(device_id)

    buf = UtteranceBuffer()
    accepting = True
    first_turn = True
    session_queries = 0
    session_started = time.perf_counter()
    pcm_frames = 0
    receive_task: asyncio.Task | None = None
    stt_task: asyncio.Task | None = None
    greet_timeout_task: asyncio.Task | None = None
    hunt_greet_timeout_s = 20.0

    def apply_listen_timeout() -> None:
        ident = get_session_identity(device_id)
        registering = ident is not None and ident.in_registration()
        buf.set_listen_max_ms(stream_listen_max_ms(in_registration=registering))

    apply_listen_timeout()

    begin_voice_session(session_id, device_id=device_id)
    log_nino_voice(
        "WS_OPEN",
        turn=voice_turn,
        device=device_id,
        session="stream",
        session_id=session_id,
        client=client_label,
        energy=aux_energy,
    )
    if not await _ws_send_json(websocket, {"type": "session", "session_id": session_id, "device_id": device_id}):
        logger.info("stream WS closed before session handshake device=%s", device_id)
    else:
        logger.info("stream WS ready device=%s session=%s", device_id, session_id)

    greet_sent = False

    async def send_session_greet() -> bool:
        nonlocal greet_sent, accepting
        if greet_sent:
            return True
        try:
            try:
                tts.set_playback_device_id(device_id)
            except Exception:
                pass
            identity_name, identity_state = await run_in_threadpool(
                _session_open_identity_snapshot, device_id
            )
            ident = get_session_identity(device_id)
            if ident is None:
                greet_sent = True
                accepting = True
                return True
            ident.set_frame_getter(cameras.frame_getter(device_id))
            open_result = ident.start_session(
                session_id=session_id,
                device_id=device_id,
                identity_name=identity_name,
                identity_state=identity_state,
            )
            if open_result.user_name:
                bind_session_user(
                    session_id, device_id=device_id, user_name=open_result.user_name
                )
                _remember_voice_viewer(open_result.user_name, device_id)
            from voice_service import synthesize_session_open_wav

            wav_out, reply_meta = await run_in_threadpool(
                lambda: synthesize_session_open_wav(
                    open_result.reply,
                    session_id=session_id,
                    device_id=device_id,
                    reply_path=open_result.reply_path,
                    eye_expression=open_result.eye_expression,
                    user_name=open_result.user_name,
                )
            )
            ok = await _voice_ws_send_reply(
                websocket,
                device_id=device_id,
                session_id=session_id,
                wav_out=wav_out,
                reply_meta=reply_meta,
                client_label=client_label,
            )
            if ok:
                greet_sent = True
                apply_listen_timeout()
                accepting = True
                logger.info(
                    "stream GREET sent device=%s session=%s wav_bytes=%s",
                    device_id,
                    session_id,
                    len(wav_out or b""),
                )
            return ok
        except Exception:
            logger.exception("stream session-open greeting failed device=%s", device_id)
            greet_sent = True
            accepting = True
            return True

    async def send_identity_refresh() -> bool:
        """After a post-TTS hunt: switch user, offer register, or stay silent."""
        try:
            names, scene_state = await run_in_threadpool(
                _live_scene_identity, device_id
            )
            ident = get_session_identity(device_id)
            if ident is None:
                return await send_skip("identity_scan")
            result = ident.apply_visible_scene(
                visible_names=names, scene_state=scene_state
            )
            if result is None:
                logger.info(
                    "stream identity_scan unchanged device=%s scene=%s names=%s",
                    device_id,
                    scene_state,
                    names,
                )
                return await send_skip("identity_unchanged")
            if result.user_name:
                bind_session_user(
                    session_id, device_id=device_id, user_name=result.user_name
                )
                if not result.is_guest:
                    _remember_voice_viewer(result.user_name, device_id)
            if not (result.reply or "").strip():
                logger.info(
                    "stream identity_scan silent user=%s guest=%s device=%s",
                    result.user_name,
                    result.is_guest,
                    device_id,
                )
                return await send_skip("identity_switched")
            from voice_service import synthesize_session_open_wav

            wav_out, reply_meta = await run_in_threadpool(
                lambda: synthesize_session_open_wav(
                    result.reply,
                    session_id=session_id,
                    device_id=device_id,
                    reply_path=result.reply_path,
                    eye_expression=result.eye_expression,
                    user_name=result.user_name,
                )
            )
            ok = await _voice_ws_send_reply(
                websocket,
                device_id=device_id,
                session_id=session_id,
                wav_out=wav_out,
                reply_meta=reply_meta,
                client_label=client_label,
            )
            if ok:
                apply_listen_timeout()
                logger.info(
                    "stream identity_scan TTS path=%s user=%s device=%s",
                    result.reply_path,
                    result.user_name,
                    device_id,
                )
            return ok
        except Exception:
            logger.exception("stream identity_scan failed device=%s", device_id)
            return await send_skip("identity_scan_error")

    async def send_look_scan(side: str) -> bool:
        """Snapshot people+objects at the current pose and speak a report."""
        pose = "left" if str(side or "").strip().lower() == "left" else "right"
        try:
            from object_detection_service import spoken_scene_report
            from voice_service import synthesize_look_scan_wav

            names, detections = await run_in_threadpool(
                lambda: _live_visible_scene(device_id, force_objects=True)
            )
            reply = spoken_scene_report(names, detections, pose=pose)
            wav_out, reply_meta = await run_in_threadpool(
                lambda: synthesize_look_scan_wav(
                    reply, session_id=session_id, device_id=device_id
                )
            )
            logger.info(
                "stream look_scan side=%s names=%s objects=%s device=%s",
                pose,
                names,
                [d.get("label") for d in (detections or [])[:8]],
                device_id,
            )
            return await _voice_ws_send_reply(
                websocket,
                device_id=device_id,
                session_id=session_id,
                wav_out=wav_out,
                reply_meta=reply_meta,
                client_label=client_label,
            )
        except Exception:
            logger.exception("stream look_scan failed device=%s side=%s", device_id, pose)
            return await send_skip("look_scan_error")

    async def send_wake_result(detected: bool) -> bool:
        return await _ws_send_json(
            websocket,
            {
                "type": "wake",
                "detected": detected,
                "wake_ok": detected,
                "session_id": session_id,
                "device_id": device_id,
                "end_session": not detected,
            },
        )

    async def send_skip(reason: str, *, continue_listen: bool = False) -> bool:
        return await _ws_send_json(
            websocket,
            {
                "type": "skip",
                "skip": True,
                "reason": reason,
                "session_id": session_id,
                "end_session": False,
                "continue_listen": bool(continue_listen),
                "prompt_medical_ack": bool(continue_listen),
            },
        )

    async def take_or_cancel_receive() -> dict | None:
        """Finish or cancel receive so send_* is never concurrent with receive()."""
        nonlocal receive_task
        task = receive_task
        receive_task = None
        if task is None:
            return None
        if not task.done():
            task.cancel()
        try:
            return await task
        except (asyncio.CancelledError, WebSocketDisconnect):
            return None
        except Exception:
            logger.exception("stream WS receive failed device=%s", device_id)
            return None

    async def build_reply(
        wav_in: bytes, session_kind: str, this_turn: int
    ) -> tuple:
        """STT/LLM/TTS only — must not touch the websocket."""
        try:
            saved = await run_in_threadpool(
                lambda audio=wav_in: save_aux_wav(
                    audio,
                    device_id=device_id,
                    turn=this_turn,
                    energy=aux_energy,
                    session=session_id[:16],
                    source="stream",
                )
            )
            logger.info(
                "Saved stream utterance %s device=%s session=%s",
                saved.relative_to(recordings_dir()).as_posix(),
                device_id,
                session_id,
            )
        except Exception:
            logger.exception("Failed to save stream utterance device=%s", device_id)
        try:
            wav_out, reply_meta, _err = await _process_voice_query_audio(
                wav_in,
                device_id=device_id,
                session_kind=session_kind,
                session_id=session_id,
                aux_energy=aux_energy,
                voice_turn=this_turn,
                client_label=client_label,
            )
        except Exception:
            logger.exception("stream STT/TTS failed device=%s", device_id)
            return ("skip", "error")
        path = str(getattr(reply_meta, "timings", {}).get("reply_path") or "")
        if path == "wake_ok":
            return ("wake_ok",)
        if path == "wake_reject":
            return ("wake_reject",)
        if path in {"stt_silent", "stt_empty", "silent_skip", "stt_rejected"} and not getattr(
            reply_meta, "end_session", False
        ):
            return ("skip", path, reply_meta)
        return ("reply", wav_out, reply_meta)

    async def apply_stt_result(result: tuple) -> bool:
        """Send skip or WAV. Receive must not be in flight. False closes the session."""
        nonlocal accepting, session_queries, greet_timeout_task
        session_queries += 1
        kind = result[0]
        if kind == "wake_ok":
            from voice_listen_state import mark_session_open

            mark_session_open(session_id, device_id)
            if not await send_wake_result(True):
                return False
            accepting = False
            await cancel_task(greet_timeout_task)
            greet_timeout_task = asyncio.create_task(asyncio.sleep(hunt_greet_timeout_s))
            logger.info(
                "stream wake ok — hunt timeout %.0fs device=%s session=%s",
                hunt_greet_timeout_s,
                device_id,
                session_id,
            )
            return True
        if kind == "wake_reject":
            await send_wake_result(False)
            return False
        if kind == "skip":
            apply_listen_timeout()
            reply_meta = result[2] if len(result) > 2 else None
            keep_listen = bool(getattr(reply_meta, "prompt_medical_ack", False))
            await send_skip(str(result[1] or "skip"), continue_listen=keep_listen)
            accepting = True
            return True
        wav_out, reply_meta = result[1], result[2]
        sent = await _voice_ws_send_reply(
            websocket,
            device_id=device_id,
            session_id=session_id,
            wav_out=wav_out,
            reply_meta=reply_meta,
            client_label=client_label,
        )
        if getattr(reply_meta, "end_session", False) or not sent:
            return False
        apply_listen_timeout()
        accepting = True
        return True

    async def finish_utterance(reason: str) -> bool:
        """Send EOS (receive not in flight). Skip now or start STT off-websocket."""
        nonlocal accepting, first_turn, voice_turn, stt_task
        ident = get_session_identity(device_id)
        registering = ident is not None and ident.in_registration()
        peak_energy = buf.vad.peak_energy
        pcm = bytes(buf.pcm)
        buf.reset()
        accepting = False
        if not await _ws_send_json(websocket, {"type": "end_of_speech", "reason": reason}):
            return False
        if reason == "timeout" and registering and ident is not None:
            guest_result = ident.timeout_to_guest()
            guest = guest_result.registered_name
            if guest:
                bind_session_user(
                    session_id, device_id=device_id, user_name=guest
                )
            logger.info(
                "stream register silence — guest device=%s session=%s user=%s",
                device_id,
                session_id,
                guest or "(none)",
            )
            apply_listen_timeout()
            await send_skip("register_timeout_guest")
            accepting = True
            first_turn = False
            return True
        if stream_idle_timeout_ends_session(reason, in_registration=registering):
            from voice_service import (
                VoiceReplyMeta,
                minimal_voice_reply_wav,
                synthesize_idle_goodbye_wav,
            )

            logger.info(
                "stream idle timeout — goodbye device=%s session=%s peak=%s",
                device_id,
                session_id,
                peak_energy,
            )
            try:
                wav_out, reply_meta = await run_in_threadpool(
                    lambda: synthesize_idle_goodbye_wav(
                        session_id=session_id, device_id=device_id
                    )
                )
            except Exception:
                logger.exception(
                    "stream idle goodbye TTS failed device=%s", device_id
                )
                reply_meta = VoiceReplyMeta(
                    end_session=True, session_id=session_id, device_id=device_id
                )
                wav_out = minimal_voice_reply_wav()
            await _voice_ws_send_reply(
                websocket,
                device_id=device_id,
                session_id=session_id,
                wav_out=wav_out,
                reply_meta=reply_meta,
                client_label=client_label,
            )
            return False
        apply_listen_timeout()
        if len(pcm) < 1600:
            await send_skip(reason or "too_short")
            accepting = True
            return True
        try:
            wav_in = pcm if is_wav_bytes(pcm) else pcm16_mono_to_wav(pcm)
        except ValueError:
            await send_skip("bad_pcm")
            accepting = True
            return True
        session_kind = "wake" if first_turn else "continue"
        first_turn = False
        voice_turn += 1
        stt_task = asyncio.create_task(build_reply(wav_in, session_kind, voice_turn))
        return True

    async def handle_chunk(chunk: bytes) -> bool:
        nonlocal pcm_frames
        if not chunk:
            return True
        if not accepting:
            # Same-task drain: drop PCM so uvicorn max_queue cannot stall TCP.
            return True
        if device_busy_speaking(device_id):
            buf.reset()
            return True
        pcm_frames += 1
        if is_wav_bytes(chunk) and len(chunk) > 4096:
            buf.reset()
            buf.pcm.extend(chunk)
            return await finish_utterance("wav")
        state = buf.feed(chunk)
        if pcm_frames == 1 or pcm_frames % 50 == 0:
            logger.info(
                "stream PCM device=%s session=%s frames=%d bytes=%d energy=%d peak=%d heard=%d start=%d continue=%d",
                device_id,
                session_id,
                pcm_frames,
                len(chunk),
                buf.vad.last_energy,
                buf.vad.peak_energy,
                int(buf.vad.heard_speech),
                buf.vad.effective_start(),
                max(buf.vad.continue_energy, buf.vad.effective_start() + 1),
            )
        if state in {"end_of_speech", "timeout"}:
            return await finish_utterance(state)
        return True

    async def handle_message(message: dict | None) -> bool:
        nonlocal greet_timeout_task
        if message is None:
            return False
        if message.get("type") == "websocket.disconnect":
            return False
        if message.get("text") is not None:
            raw = str(message.get("text") or "").strip()
            if not raw:
                return True
            try:
                payload = json.loads(raw)
            except ValueError:
                return True
            if str(payload.get("type") or "").strip().lower() == "ready":
                logger.info(
                    "stream hunt ready — GREET device=%s session=%s",
                    device_id,
                    session_id,
                )
                await cancel_task(greet_timeout_task)
                greet_timeout_task = None
                return await send_session_greet()
            if str(payload.get("type") or "").strip().lower() == "identity_scan":
                logger.info(
                    "stream identity_scan device=%s session=%s",
                    device_id,
                    session_id,
                )
                return await send_identity_refresh()
            if str(payload.get("type") or "").strip().lower() == "look_scan":
                side = str(payload.get("side") or "").strip().lower()
                logger.info(
                    "stream look_scan side=%s device=%s session=%s",
                    side or "right",
                    device_id,
                    session_id,
                )
                return await send_look_scan(side)
            return True
        chunk = message.get("bytes")
        if not chunk:
            return True
        return await handle_chunk(chunk)

    async def cancel_task(task: asyncio.Task | None) -> None:
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except (asyncio.CancelledError, WebSocketDisconnect, Exception):
            pass

    try:
        if first_chunk is not None and not await handle_chunk(first_chunk):
            return
        receive_task = asyncio.create_task(websocket.receive())
        while True:
            if receive_task is None:
                receive_task = asyncio.create_task(websocket.receive())
            wait_set = {receive_task}
            if stt_task is not None:
                wait_set.add(stt_task)
            if greet_timeout_task is not None:
                wait_set.add(greet_timeout_task)
            done, _pending = await asyncio.wait(
                wait_set, return_when=asyncio.FIRST_COMPLETED
            )
            if greet_timeout_task is not None and greet_timeout_task in done:
                greet_timeout_task = None
                leftover = await take_or_cancel_receive()
                if leftover is not None and leftover.get("type") == "websocket.disconnect":
                    break
                logger.info(
                    "stream hunt timeout — GREET device=%s session=%s",
                    device_id,
                    session_id,
                )
                try:
                    keep = await send_session_greet()
                except (WebSocketDisconnect, Exception):
                    logger.exception(
                        "stream WS send failed after hunt timeout device=%s",
                        device_id,
                    )
                    break
                if leftover is not None and not await handle_message(leftover):
                    break
                if not keep:
                    break
                receive_task = asyncio.create_task(websocket.receive())
                continue
            if stt_task is not None and stt_task in done:
                try:
                    result = stt_task.result()
                except Exception:
                    logger.exception("stream STT task failed device=%s", device_id)
                    result = ("skip", "error")
                stt_task = None
                leftover = await take_or_cancel_receive()
                if leftover is not None and leftover.get("type") == "websocket.disconnect":
                    break
                try:
                    keep = await apply_stt_result(result)
                except (WebSocketDisconnect, Exception):
                    logger.exception(
                        "stream WS send failed after STT device=%s", device_id
                    )
                    break
                if not keep:
                    break
                receive_task = asyncio.create_task(websocket.receive())
                continue
            try:
                message = receive_task.result()
            except WebSocketDisconnect:
                break
            except Exception:
                logger.exception("stream WS receive failed device=%s", device_id)
                break
            receive_task = None
            if not await handle_message(message):
                break
            receive_task = asyncio.create_task(websocket.receive())
    finally:
        accepting = False
        await cancel_task(receive_task)
        await cancel_task(stt_task)
        await cancel_task(greet_timeout_task)
        await run_in_threadpool(
            _append_latency_record,
            _latency_log_record(
                event="ws_closed",
                client=client_label,
                device_id=device_id,
                session_id=session_id,
                session_queries=session_queries,
                session_seconds=round(time.perf_counter() - session_started, 3),
            ),
        )
        try:
            await websocket.close()
        except Exception:
            pass
        ident = get_session_identity(device_id)
        if ident is not None:
            ident.end_session()


async def _voice_ws_pipeline(websocket: WebSocket, device_id: str) -> None:
    """STT (WAV or 16 kHz mono PCM) → wake validate (if needed) → Ollama → SAPI WAV."""
    await websocket.accept()
    from voice_service import (
        SERVO_360_TRIGGER_DELAY_SECONDS,
        VoiceReplyMeta,
        log_nino_voice,
        minimal_voice_reply_wav,
        process_voice_wav,
    )

    session_kind = _session_kind_from_websocket(websocket)
    aux_energy = _aux_energy_from_websocket(websocket)
    voice_turn = _voice_turn_from_websocket(websocket)
    session_queries = 0
    session_started = time.perf_counter()
    client_label = _ws_client_label(websocket)
    log_nino_voice(
        "WS_OPEN",
        turn=voice_turn,
        device=device_id,
        session=session_kind,
        client=client_label,
        energy=aux_energy,
    )
    pipeline_log(
        "DEVICE",
        "CONNECT",
        device_id=device_id,
        turn=voice_turn,
        session=session_kind,
        client=client_label,
        energy=aux_energy,
        path=str(websocket.url.path if hasattr(websocket, "url") else "/ws/voice"),
    )
    await run_in_threadpool(
        _append_latency_record,
        _latency_log_record(
            event="ws_open", client=client_label, device_id=device_id
        ),
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
                        device_id=device_id,
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
                        device_id=device_id,
                        error="WebSocket frame had no audio bytes.",
                    ),
                )
                continue

            if looks_like_stream_pcm_frame(wav_in):
                logger.info(
                    "Voice WS upgrading to stream pipeline (%d byte PCM frame) device=%s",
                    len(wav_in),
                    device_id,
                )
                await _voice_ws_stream_pipeline(
                    websocket,
                    device_id,
                    already_accepted=True,
                    first_chunk=wav_in,
                )
                return

            try:
                saved = await run_in_threadpool(
                    lambda audio=wav_in: save_aux_wav(
                        audio,
                        device_id=device_id,
                        turn=voice_turn,
                        energy=aux_energy,
                        session=session_kind,
                        source="query",
                    )
                )
                logger.info(
                    "Saved voice-query WAV %s (%d bytes) device=%s",
                    saved.relative_to(recordings_dir()).as_posix(),
                    len(wav_in),
                    device_id,
                )
            except Exception:
                logger.exception("Failed to save incoming Aux-in WAV device=%s", device_id)

            wav_out: bytes | None = None
            reply_meta = VoiceReplyMeta(device_id=device_id)
            active_viewer: str | None = None
            t_query_start = time.perf_counter()
            identity_seconds = 0.0
            pipeline_error: str | None = None
            begin_pipeline(
                device_id=device_id,
                turn=voice_turn,
                session=session_kind,
                t0=t_query_start,
            )
            pipeline_log(
                "DEVICE",
                "AUDIO",
                device_id=device_id,
                turn=voice_turn,
                session=session_kind,
                t0=t_query_start,
                bytes=len(wav_in),
                client=client_label,
                energy=aux_energy,
            )
            await run_in_threadpool(begin_voice_query, device_id)
            try:
                try:
                    t_ident = time.perf_counter()
                    identity_name, identity_state = await run_in_threadpool(
                        _camera_identity_snapshot, True, device_id
                    )
                    if identity_state == "recognized" and identity_name:
                        normalized_identity = str(identity_name).strip()
                        if normalized_identity and normalized_identity.lower() not in {
                            "unknown",
                            "face",
                        }:
                            active_viewer = normalized_identity
                    if not active_viewer:
                        # Face can briefly drop during continue-listen; keep chat continuity.
                        active_viewer = await run_in_threadpool(
                            _recall_voice_viewer, device_id
                        )
                    identity_seconds = round(time.perf_counter() - t_ident, 3)
                    pipeline_log(
                        "IDENT",
                        "DONE",
                        device_id=device_id,
                        turn=voice_turn,
                        session=session_kind,
                        t0=t_query_start,
                        name=identity_name or "(none)",
                        state=identity_state,
                        viewer=active_viewer or "(none)",
                        stage_s=identity_seconds,
                    )

                    camera_scene = await run_in_threadpool(
                        _camera_scene_snapshot, device_id
                    )
                    visible_names = _recognized_viewer_names(
                        _cached_face_results(device_id), allow_pending=True
                    )

                    def _run_voice() -> tuple[bytes, VoiceReplyMeta]:
                        return process_voice_wav(
                            wav_in,
                            active_viewer,
                            camera_identity_name=identity_name,
                            camera_identity_state=identity_state,
                            camera_scene=camera_scene,
                            visible_names=visible_names,
                            device_id=device_id,
                            session_kind=session_kind,
                            session_id=_session_id_from_websocket(websocket),
                            aux_energy=aux_energy,
                            voice_turn=voice_turn,
                            pipeline_t0=t_query_start,
                        )

                    wav_out, reply_meta = await run_in_threadpool(_run_voice)
                except Exception as exc:
                    logger.exception("Voice pipeline failed device=%s", device_id)
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
                timings.setdefault("session", session_kind)
                server_total = round(time.perf_counter() - t_query_start, 3)
                process_total = float(timings.get("process_total_seconds") or 0)
                timings["identity_seconds"] = identity_seconds
                timings["server_total_seconds"] = server_total
                timings["overhead_seconds"] = round(
                    max(0.0, server_total - process_total), 3
                )
                log_nino_voice(
                    "DONE",
                    turn=timings.get("turn", voice_turn),
                    device=device_id,
                    session=session_kind,
                    path=timings.get("reply_path"),
                    wake_ok=timings.get("wake_ok"),
                    continue_listen=int(bool(reply_meta.prompt_medical_ack)),
                    heard=str(timings.get("heard") or "")[:120] or "(empty)",
                    reply=str(timings.get("reply_text") or "")[:120],
                    stt=float(timings.get("stt_seconds") or 0),
                    llm=float(timings.get("reply_seconds") or 0),
                    tts=float(timings.get("tts_seconds") or 0),
                    process=process_total,
                    identity=identity_seconds,
                    server=server_total,
                    in_s=float(timings.get("audio_in_seconds") or 0),
                    out_s=float(timings.get("audio_out_seconds") or 0),
                )
                pipeline_log(
                    "TOTAL",
                    "SERVER",
                    device_id=device_id,
                    turn=timings.get("turn", voice_turn),
                    session=session_kind,
                    t0=t_query_start,
                    path=timings.get("reply_path"),
                    asr_s=float(timings.get("stt_seconds") or 0),
                    llm_s=float(timings.get("reply_seconds") or 0),
                    tts_s=float(timings.get("tts_seconds") or 0),
                    identity_s=identity_seconds,
                    process_s=process_total,
                    heard=str(timings.get("heard") or "")[:120] or "(empty)",
                    reply=str(timings.get("reply_text") or "")[:120],
                    stage_s=server_total,
                )
                record = _latency_log_record(
                    event="voice_query",
                    client=client_label,
                    device_id=device_id,
                    **timings,
                )
                if pipeline_error is not None:
                    record["error"] = pipeline_error
                await run_in_threadpool(_append_latency_record, record)
                session_queries += 1

                if reply_meta.registered_face_name:
                    _remember_voice_viewer(
                        reply_meta.registered_face_name, device_id
                    )

                await run_in_threadpool(
                    tts.notify_voice_interaction, active_viewer, device_id
                )

                ws_meta: dict[str, object] = {
                    "prompt_medical_ack": reply_meta.prompt_medical_ack,
                    "end_session": bool(reply_meta.end_session),
                    "device_id": device_id,
                    "session_id": reply_meta.session_id,
                }
                eye_tag = normalize_eye_expression(reply_meta.eye_expression)
                if eye_tag:
                    ws_meta["eye_expression"] = eye_tag
                    logger.info(
                        "WS send eye_expression=%s device=%s client=%s",
                        eye_tag,
                        device_id,
                        client_label,
                    )
                if getattr(reply_meta, "look_scan", False):
                    ws_meta["look_scan"] = True
                if getattr(reply_meta, "face_track", None) is not None:
                    ws_meta["face_track"] = bool(reply_meta.face_track)
                await websocket.send_json(ws_meta)
                if wav_out:
                    t_send = time.perf_counter()
                    await websocket.send_bytes(wav_out)
                    pipeline_log(
                        "DEVICE",
                        "SEND",
                        device_id=device_id,
                        turn=timings.get("turn", voice_turn),
                        session=session_kind,
                        t0=t_query_start,
                        wav_bytes=len(wav_out),
                        continue_listen=int(bool(reply_meta.prompt_medical_ack)),
                        stage_s=time.perf_counter() - t_send,
                    )

                if reply_meta.trigger_servo_360:
                    asyncio.create_task(
                        _delayed_esp_servo_360(
                            SERVO_360_TRIGGER_DELAY_SECONDS, device_id
                        )
                    )
            except (WebSocketDisconnect, RuntimeError):
                await run_in_threadpool(
                    _append_latency_record,
                    _latency_log_record(
                        event="ws_disconnect_during_send",
                        client=client_label,
                        device_id=device_id,
                        heard=reply_meta.timings.get("heard", "")[:200],
                    ),
                )
                break
            finally:
                await run_in_threadpool(end_voice_query, device_id)
    finally:
        if session_queries == 0:
            await run_in_threadpool(
                _append_latency_record,
                _latency_log_record(
                    event="ws_closed_without_audio",
                    client=client_label,
                    device_id=device_id,
                    session_seconds=round(time.perf_counter() - session_started, 3),
                ),
            )
            pipeline_log(
                "DEVICE",
                "DISCONNECT",
                device_id=device_id,
                turn=voice_turn,
                session=session_kind,
                client=client_label,
                queries=0,
                stage_s=time.perf_counter() - session_started,
            )
        else:
            await run_in_threadpool(
                _append_latency_record,
                _latency_log_record(
                    event="ws_closed",
                    client=client_label,
                    device_id=device_id,
                    session_queries=session_queries,
                    session_seconds=round(time.perf_counter() - session_started, 3),
                ),
            )
            pipeline_log(
                "DEVICE",
                "DISCONNECT",
                device_id=device_id,
                turn=voice_turn,
                session=session_kind,
                client=client_label,
                queries=session_queries,
                stage_s=time.perf_counter() - session_started,
            )
        end_pipeline()
        try:
            await websocket.close()
        except Exception:
            pass


@app.websocket("/ws/voice")
async def voice_websocket(websocket: WebSocket) -> None:
    device_id = _device_id_from_websocket(websocket)
    if _voice_wants_stream(websocket):
        await _voice_ws_stream_pipeline(websocket, device_id)
        return
    await _voice_ws_pipeline(websocket, device_id)


@app.websocket("/voice-query")
async def voice_query_websocket(websocket: WebSocket) -> None:
    """Same pipeline as /ws/voice (ESP32 voice_optimized URI compatibility)."""
    device_id = _device_id_from_websocket(websocket)
    if _voice_wants_stream(websocket):
        await _voice_ws_stream_pipeline(websocket, device_id)
        return
    await _voice_ws_pipeline(websocket, device_id)


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
        "--whisper-device",
        default=os.environ.get("WHISPER_DEVICE", "cuda"),
        choices=["", "auto", "cuda", "gpu", "cpu"],
        help="faster-whisper device: cuda (default), auto, gpu, or cpu.",
    )
    parser.add_argument(
        "--whisper-compute-type",
        default=os.environ.get("WHISPER_COMPUTE_TYPE", "auto"),
        help="faster-whisper compute type (auto=float16 on CUDA, int8 on CPU).",
    )
    parser.add_argument(
        "--stt-provider",
        default=os.environ.get("STT_PROVIDER", ""),
        choices=["", "whisper", "elevenlabs", "openai_whisper", "openai", "whisper_api"],
        help="STT engine: openai_whisper (cloud Whisper API), elevenlabs (Scribe), "
        "or whisper (local faster-whisper). "
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
        choices=["", "elevenlabs", "piper", "sapi", "local"],
        help=(
            "TTS engine: elevenlabs (cloud, default when API key set), "
            "piper (local neural), sapi (Windows), or local (espeak-ng)."
        ),
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
    if args.camera_rotation.strip():
        os.environ["CAMERA_ROTATION"] = args.camera_rotation.strip()

    if args.esp_play_wav_url.strip():
        os.environ["ESP_PLAY_WAV_URL"] = args.esp_play_wav_url.strip()
    ensure_esp_play_wav_url_configured(camera_source=camera_source)
    registry.reload()
    cameras.configure_from_registry()
    # CLI camera source applies to the UI / default device when using legacy fallback.
    try:
        cameras.restart(registry.ui_device_id(), camera_source)
    except Exception as exc:
        logger.warning("Could not apply CLI camera source: %s", exc)
    tts.set_playback_device_id(registry.ui_device_id())
    face_registration.set_device_id(registry.ui_device_id())
    face_registration.set_frame_getter(cameras.frame_getter(registry.ui_device_id()))

    if args.alarm_wav.strip():
        os.environ["ALARM_WAV_PATH"] = args.alarm_wav.strip()

    from llm_service import resolve_ollama_api_url, try_start_gpu_ollama

    try_start_gpu_ollama()
    ollama_url = resolve_ollama_api_url(
        model=args.ollama_model.strip(),
        preferred=args.ollama_url,
    )
    os.environ["OLLAMA_URL"] = ollama_url
    os.environ["OLLAMA_MODEL"] = args.ollama_model.strip()
    os.environ["WHISPER_MODEL"] = args.whisper_model.strip()
    if args.whisper_device.strip():
        os.environ["WHISPER_DEVICE"] = args.whisper_device.strip().lower()
    if args.whisper_compute_type.strip():
        os.environ["WHISPER_COMPUTE_TYPE"] = args.whisper_compute_type.strip()
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
