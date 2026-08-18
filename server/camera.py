from __future__ import annotations

import os
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlparse, urlunparse

os.environ.setdefault("OPENCV_LOG_LEVEL", "SILENT")

import cv2
import numpy as np

from pipeline_log import pipeline_log

try:
    cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
except Exception:
    pass

AUTO_CAMERA_SOURCE = "auto"
AUTO_CAMERA_INDEXES = range(0, 8)
# UI / status: treat camera as connected if a frame arrived within this window (covers bad JPEG skips).
STATUS_STALE_FRAME_SECONDS = 4.0
SNAPSHOT_HTTP_TIMEOUT_SECONDS = 3.0
SNAPSHOT_POLL_INTERVAL_SECONDS = 0.04
_JPEG_DECODE_LOCK = threading.Lock()


def parse_camera_rotation(raw: str | None) -> int | None:
    """Map CAMERA_ROTATION env/CLI value to an OpenCV rotate code."""
    normalized = normalize_camera_rotation(raw)
    if normalized in {None, "none"}:
        return None
    if normalized == "cw90":
        return cv2.ROTATE_90_CLOCKWISE
    if normalized == "ccw90":
        return cv2.ROTATE_90_COUNTERCLOCKWISE
    if normalized == "180":
        return cv2.ROTATE_180
    return None


def normalize_camera_rotation(raw: str | None) -> str | None:
    """Return a persisted orientation label, or ``None`` for invalid input."""
    if not raw or not str(raw).strip():
        return "none"
    key = str(raw).strip().lower()
    if key in {"0", "none", "off", "normal"}:
        return "none"
    if key in {"90", "cw", "cw90", "clockwise", "right"}:
        return "cw90"
    if key in {"-90", "270", "ccw", "ccw90", "counterclockwise", "counter-clockwise", "left"}:
        return "ccw90"
    if key in {"180", "flip"}:
        return "180"
    return None


def _rotation_label(rotate_code: int | None) -> str:
    if rotate_code == cv2.ROTATE_90_CLOCKWISE:
        return "cw90"
    if rotate_code == cv2.ROTATE_90_COUNTERCLOCKWISE:
        return "ccw90"
    if rotate_code == cv2.ROTATE_180:
        return "180"
    return "none"


def _extract_first_jpeg(data: bytes) -> bytes | None:
    """Keep one JPEG (SOI … EOI). Drops leading/trailing HTTP noise; avoids duplicate images."""
    if not data:
        return None
    start = data.find(b"\xff\xd8")
    if start < 0:
        return None
    end = data.find(b"\xff\xd9", start + 2)
    if end < 0:
        return data[start:]
    return data[start : end + 2]


@contextmanager
def _suppress_native_stderr() -> Any:
    """Silence libjpeg/OpenCV C-level warnings for one decode call.

    These warnings are noisy (e.g. \"Corrupt JPEG data: ...\") and often benign for
    streaming cameras. Suppression is scoped and serialized by a lock.
    """
    with _JPEG_DECODE_LOCK:
        stderr_fd = None
        saved_fd = None
        devnull_fd = None
        try:
            stderr_fd = sys.stderr.fileno()
            saved_fd = os.dup(stderr_fd)
            devnull_fd = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull_fd, stderr_fd)
            yield
        finally:
            if saved_fd is not None and stderr_fd is not None:
                os.dup2(saved_fd, stderr_fd)
            if devnull_fd is not None:
                os.close(devnull_fd)
            if saved_fd is not None:
                os.close(saved_fd)


class CameraStream:
    """Continuously pulls frames from a local camera index or stream URL."""

    def __init__(self, default_source: str, rotation: str | None = None, device_id: str = "") -> None:
        self.source = default_source
        self.device_id = device_id or "-"
        self.active_source: int | str | None = None
        self._capture: cv2.VideoCapture | None = None
        self._frame: np.ndarray | None = None
        self._lock = threading.Lock()
        self._capture_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._connected = False
        self._last_error = ""
        self._last_frame_at = 0.0
        self._frames_received = 0
        self._rotation_code: int | None = None
        self._rotation_setting = rotation

    def start(self, source: str | None = None) -> None:
        if source:
            self.source = source
        rotation = (
            self._rotation_setting
            if self._rotation_setting is not None
            else os.getenv("CAMERA_ROTATION", "")
        )
        self._rotation_code = parse_camera_rotation(rotation)

        if self._thread and self._thread.is_alive():
            # A prior stop() may still be draining (e.g. blocked in urlopen).
            # Never return early while stop is requested — that leaves no worker.
            if not self._stop_event.is_set():
                return
            self._join_worker(SNAPSHOT_HTTP_TIMEOUT_SECONDS + 1.0)

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="camera-stream", daemon=True)
        self._thread.start()
        pipeline_log(
            "CAMERA",
            "START",
            device_id=self.device_id,
            source=self.source,
        )

    def stop(self) -> None:
        self._stop_event.set()
        self._release_capture()
        # Wait long enough for an in-flight snapshot HTTP call to time out.
        self._join_worker(SNAPSHOT_HTTP_TIMEOUT_SECONDS + 1.0)
        self._connected = False

    def restart(self, source: str) -> None:
        self.stop()
        self.start(source)

    def set_rotation(self, rotation: str) -> str:
        normalized = normalize_camera_rotation(rotation)
        if normalized is None:
            raise ValueError(f"Unsupported camera rotation: {rotation!r}")
        self._rotation_setting = normalized
        self._rotation_code = parse_camera_rotation(normalized)
        return normalized

    def _mark_connected(self, connected: bool, source: object = "", *, error: str = "") -> None:
        was = self._connected
        self._connected = connected
        if error:
            self._last_error = error
        if was == connected:
            return
        pipeline_log(
            "CAMERA",
            "UP" if connected else "DOWN",
            device_id=self.device_id,
            source=source or self.active_source or self.source,
            error=error or None,
        )

    def _join_worker(self, timeout: float) -> None:
        if (
            self._thread
            and self._thread.is_alive()
            and threading.current_thread() is not self._thread
        ):
            try:
                self._thread.join(timeout=timeout)
            except BaseException:
                pass

    def read(self) -> np.ndarray | None:
        with self._lock:
            if self._frame is None:
                return None
            return self._frame.copy()

    def status(self) -> dict[str, Any]:
        age = None
        if self._last_frame_at > 0:
            age = round(time.time() - self._last_frame_at, 2)

        transport = "opencv"
        if self.active_source and isinstance(self.active_source, str):
            if self.active_source.lower().find("snapshot") >= 0:
                transport = "http_snapshot"

        now = time.time()
        if self._frames_received > 0 and self._last_frame_at > 0.0:
            connected_ui = (
                now - self._last_frame_at
            ) < STATUS_STALE_FRAME_SECONDS
        else:
            connected_ui = self._connected

        return {
            "source": self.source,
            "active_source": self.active_source,
            "transport": transport,
            "rotation": _rotation_label(self._rotation_code),
            "connected": connected_ui,
            "frames_received": self._frames_received,
            "last_frame_age_seconds": age,
            "last_error": self._last_error,
        }

    def _store_frame(self, frame: np.ndarray) -> None:
        if self._rotation_code is not None:
            frame = cv2.rotate(frame, self._rotation_code)
        with self._lock:
            self._frame = frame

    def _http_snapshot_poll_url(self, raw: str) -> str | None:
        """ESP32 (and similar) HTTP servers: one MJPEG /stream client can starve OpenCV.
        Polling /snapshot.jpg avoids competing with the browser and is much more reliable.
        """
        s = raw.strip()
        parsed = urlparse(s)
        if parsed.scheme not in ("http", "https"):
            return None
        path = (parsed.path or "").rstrip("/").lower()
        if path.endswith("/snapshot.jpg") or path.endswith("/snapshot"):
            return s
        if path.endswith("/stream"):
            new_path = (parsed.path or "").rsplit("/", 1)[0] + "/snapshot.jpg"
            return urlunparse(
                (
                    parsed.scheme,
                    parsed.netloc,
                    new_path,
                    parsed.params,
                    parsed.query,
                    parsed.fragment,
                )
            )
        return None

    def _run_http_snapshot_loop(self, snapshot_url: str) -> None:
        self._release_capture()
        while not self._stop_event.is_set():
            try:
                req = urllib.request.Request(
                    snapshot_url,
                    headers={"Cache-Control": "no-cache", "Connection": "close"},
                )
                with urllib.request.urlopen(req, timeout=SNAPSHOT_HTTP_TIMEOUT_SECONDS) as resp:
                    data = resp.read()
                if not data:
                    raise ValueError("empty snapshot response")
                jpeg = _extract_first_jpeg(data)
                if not jpeg:
                    raise ValueError("response is not a JPEG")
                arr = np.frombuffer(jpeg, dtype=np.uint8)
                with _suppress_native_stderr():
                    frame = cv2.imdecode(
                        arr, cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION
                    )
                if frame is None:
                    raise ValueError("snapshot is not a valid JPEG")
                self._store_frame(frame)
                self._frames_received += 1
                self._last_frame_at = time.time()
                self._mark_connected(True, snapshot_url)
                self._last_error = ""
                self.active_source = snapshot_url
            except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
                self._mark_connected(False, snapshot_url, error=str(exc)[:240])
            except ValueError as exc:
                # Bad or partial JPEG: keep last good frame; do not flip "connected" off
                # if we were already streaming (avoids UI flicker with MJPEG-style JPEGs).
                self._last_error = str(exc)[:120]
                if self._frames_received == 0:
                    self._mark_connected(False, snapshot_url, error=str(exc)[:120])
            except Exception as exc:
                # Keep the poller alive; a single decode/FD glitch must not freeze the UI.
                self._mark_connected(False, snapshot_url, error=str(exc)[:240])
            time.sleep(SNAPSHOT_POLL_INTERVAL_SECONDS)

        self._mark_connected(False, snapshot_url)

    def _run(self) -> None:
        snapshot_url = self._http_snapshot_poll_url(self.source)
        if snapshot_url:
            self._run_http_snapshot_loop(snapshot_url)
            return

        while not self._stop_event.is_set():
            with self._capture_lock:
                capture = self._capture

            if capture is None or not capture.isOpened():
                self._open_capture()
                with self._capture_lock:
                    capture = self._capture
                if capture is None or not capture.isOpened():
                    time.sleep(1.0)
                    continue

            ok, frame = capture.read()
            if not ok or frame is None:
                self._mark_connected(False, error="Could not read frame; reconnecting")
                self._release_capture()
                time.sleep(0.5)
                continue

            self._store_frame(frame)
            self._frames_received += 1
            self._last_frame_at = time.time()
            self._mark_connected(True, self.active_source)
            self._last_error = ""

        self._mark_connected(False)

    def _open_capture(self) -> None:
        self._release_capture()
        candidates = self._opencv_sources()

        for source in candidates:
            capture = self._make_capture(source)
            if not capture.isOpened():
                capture.release()
                continue

            ok, frame = False, None
            if isinstance(source, str) and source.lower().startswith(
                ("http://", "https://")
            ):
                deadline = time.time() + 30.0
                while time.time() < deadline:
                    ok, frame = capture.read()
                    if ok and frame is not None:
                        break
                    time.sleep(0.1)
            else:
                ok, frame = capture.read()

            if ok and frame is not None:
                self._store_frame(frame)
                self.active_source = source
                with self._capture_lock:
                    self._capture = capture
                return

            capture.release()

        if self.source.strip().lower() == AUTO_CAMERA_SOURCE:
            self._last_error = "Could not open any local camera source from 0 to 7"
        else:
            self._last_error = f"Could not open camera source: {self.source}"
        self.active_source = None
        self._mark_connected(False, error=self._last_error)

    def _make_capture(self, source: int | str) -> cv2.VideoCapture:
        if isinstance(source, int):
            capture = cv2.VideoCapture(source, cv2.CAP_DSHOW)
            capture.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            return capture

        url = str(source)
        if url.lower().startswith(("http://", "https://")):
            # MSMF default often fails or times out on MJPEG over HTTP; FFmpeg works.
            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = (
                "stimeout;20000000|rw_timeout;20000000"
            )
            capture = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
            capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            return capture

        return cv2.VideoCapture(url)

    def _opencv_sources(self) -> list[int | str]:
        source = self.source.strip()
        if source.lower() == AUTO_CAMERA_SOURCE:
            return list(AUTO_CAMERA_INDEXES)
        if source.isdigit():
            return [int(source)]
        return [source]

    def _release_capture(self) -> None:
        with self._capture_lock:
            capture = self._capture
            self._capture = None

        if capture is not None:
            try:
                capture.release()
            except Exception as exc:
                self._last_error = f"Could not release camera cleanly: {exc}"


class CameraPool:
    """Per-device CameraStream pool keyed by device_id."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._streams: dict[str, CameraStream] = {}

    def configure_from_registry(self) -> None:
        from device_registry import get_device_registry

        registry = get_device_registry()
        wanted: dict[str, str] = {}
        for record in registry.list_devices():
            cam = record.effective_camera_url()
            if cam:
                wanted[record.device_id] = cam
        with self._lock:
            for device_id in list(self._streams.keys()):
                if device_id not in wanted:
                    self._streams[device_id].stop()
                    del self._streams[device_id]
            for device_id, source in wanted.items():
                existing = self._streams.get(device_id)
                if existing is None:
                    record = registry.get(device_id)
                    stream = CameraStream(
                        source,
                        record.camera_rotation if record else "none",
                        device_id=device_id,
                    )
                    stream.start()
                    self._streams[device_id] = stream
                else:
                    record = registry.get(device_id)
                    if record:
                        existing.set_rotation(record.camera_rotation)
                    if existing.source != source:
                        existing.restart(source)

    def start_all(self) -> None:
        self.configure_from_registry()

    def stop_all(self) -> None:
        with self._lock:
            for stream in self._streams.values():
                stream.stop()
            self._streams.clear()

    def ensure(self, device_id: str | None) -> CameraStream:
        from device_registry import get_device_registry

        record = get_device_registry().resolve_or_default(device_id)
        source = record.effective_camera_url() or "auto"
        with self._lock:
            stream = self._streams.get(record.device_id)
            if stream is None:
                stream = CameraStream(source, record.camera_rotation, device_id=record.device_id)
                stream.start()
                self._streams[record.device_id] = stream
            else:
                stream.set_rotation(record.camera_rotation)
                if stream.source != source and source:
                    stream.restart(source)
            return stream

    def read(self, device_id: str | None = None) -> np.ndarray | None:
        return self.ensure(device_id).read()

    def restart(self, device_id: str | None, source: str) -> CameraStream:
        from device_registry import get_device_registry

        record = get_device_registry().resolve_or_default(device_id)
        with self._lock:
            stream = self._streams.get(record.device_id)
            if stream is None:
                stream = CameraStream(source, record.camera_rotation, device_id=record.device_id)
                stream.start()
                self._streams[record.device_id] = stream
            else:
                stream.restart(source)
            return stream

    def set_rotation(self, device_id: str | None, rotation: str) -> CameraStream:
        stream = self.ensure(device_id)
        stream.set_rotation(rotation)
        return stream

    def status(self, device_id: str | None = None) -> dict[str, Any]:
        if device_id is not None:
            stream = self.ensure(device_id)
            payload = stream.status()
            payload["device_id"] = get_device_registry_safe_id(device_id)
            return payload
        with self._lock:
            return {
                device_id: stream.status()
                for device_id, stream in self._streams.items()
            }

    def frame_getter(self, device_id: str | None):
        def _read() -> np.ndarray | None:
            return self.read(device_id)

        return _read


def get_device_registry_safe_id(device_id: str | None) -> str:
    from device_registry import resolve_device_id

    return resolve_device_id(device_id)
