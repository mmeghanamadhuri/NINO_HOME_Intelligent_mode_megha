"""Multi-robot device registry — maps device_id → camera / playback URLs."""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DEVICES_PATH = BASE_DIR / "data" / "devices.json"
LEGACY_DEVICE_ID = "default"


@dataclass(frozen=True)
class DeviceRecord:
    device_id: str
    display_name: str = ""
    camera_url: str = ""
    play_wav_url: str = ""
    base_url: str = ""

    def effective_base_url(self) -> str:
        if self.base_url.strip():
            return self.base_url.rstrip("/")
        for candidate in (self.play_wav_url, self.camera_url):
            raw = (candidate or "").strip()
            if not raw.lower().startswith(("http://", "https://")):
                continue
            parsed = urllib.parse.urlparse(raw)
            if parsed.scheme and parsed.netloc:
                return f"{parsed.scheme}://{parsed.netloc}"
        return ""

    def effective_play_wav_url(self) -> str:
        if self.play_wav_url.strip():
            return self.play_wav_url.strip()
        base = self.effective_base_url()
        return f"{base}/play_wav" if base else ""

    def effective_camera_url(self) -> str:
        if self.camera_url.strip():
            return self.camera_url.strip()
        base = self.effective_base_url()
        return f"{base}/stream" if base else ""


class DeviceRegistry:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path or DEFAULT_DEVICES_PATH
        self._lock = threading.RLock()
        self._devices: dict[str, DeviceRecord] = {}
        self._ui_device_id: str = LEGACY_DEVICE_ID
        self.reload()

    def reload(self) -> None:
        loaded = self._load_from_file()
        if not loaded:
            loaded = self._legacy_from_environ()
            if loaded:
                logger.info(
                    "Device registry empty — using legacy CAMERA_SOURCE / ESP_PLAY_WAV_URL "
                    "as device_id=%s",
                    LEGACY_DEVICE_ID,
                )
        with self._lock:
            self._devices = {d.device_id: d for d in loaded}
            if self._ui_device_id not in self._devices and self._devices:
                self._ui_device_id = next(iter(self._devices))
            elif not self._devices:
                self._ui_device_id = LEGACY_DEVICE_ID
        logger.info(
            "Device registry loaded: %d device(s) %s",
            len(self._devices),
            list(self._devices.keys()),
        )

    def _load_from_file(self) -> list[DeviceRecord]:
        if not self._path.is_file():
            return []
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Could not read %s: %s", self._path, exc)
            return []
        items = raw.get("devices", raw) if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            return []
        out: list[DeviceRecord] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            device_id = str(item.get("device_id") or "").strip()
            if not device_id:
                continue
            out.append(
                DeviceRecord(
                    device_id=device_id,
                    display_name=str(item.get("display_name") or "").strip(),
                    camera_url=str(item.get("camera_url") or "").strip(),
                    play_wav_url=str(item.get("play_wav_url") or "").strip(),
                    base_url=str(item.get("base_url") or "").strip(),
                )
            )
        return out

    @staticmethod
    def _legacy_from_environ() -> list[DeviceRecord]:
        camera = os.environ.get("CAMERA_SOURCE", "").strip()
        play = os.environ.get("ESP_PLAY_WAV_URL", "").strip()
        if not camera and not play:
            # Local webcam / auto still counts as one legacy device.
            camera = os.environ.get("CAMERA_STREAM_URL", "auto").strip() or "auto"
        base = ""
        for candidate in (play, camera):
            if candidate.lower().startswith(("http://", "https://")):
                parsed = urllib.parse.urlparse(candidate)
                if parsed.scheme and parsed.netloc:
                    base = f"{parsed.scheme}://{parsed.netloc}"
                    break
        if not play and base:
            play = f"{base}/play_wav"
        return [
            DeviceRecord(
                device_id=LEGACY_DEVICE_ID,
                display_name="Default",
                camera_url=camera,
                play_wav_url=play,
                base_url=base,
            )
        ]

    def list_devices(self) -> list[DeviceRecord]:
        with self._lock:
            return list(self._devices.values())

    def get(self, device_id: str | None) -> DeviceRecord | None:
        key = (device_id or "").strip()
        if not key:
            return None
        with self._lock:
            return self._devices.get(key)

    def default_device_id(self) -> str:
        with self._lock:
            if self._ui_device_id in self._devices:
                return self._ui_device_id
            if self._devices:
                return next(iter(self._devices))
            return LEGACY_DEVICE_ID

    def resolve_or_default(self, device_id: str | None) -> DeviceRecord:
        """Return the named device, or the UI/default device. Never raises."""
        with self._lock:
            key = (device_id or "").strip()
            if key and key in self._devices:
                return self._devices[key]
            if not key:
                pass
            elif key not in self._devices:
                logger.warning(
                    "Unknown device_id=%r — falling back to %s",
                    key,
                    self.default_device_id(),
                )
            fallback_id = self.default_device_id()
            if fallback_id in self._devices:
                return self._devices[fallback_id]
        # Empty registry — synthesize legacy on the fly.
        legacy = self._legacy_from_environ()
        return legacy[0]

    def set_ui_device_id(self, device_id: str) -> str:
        key = (device_id or "").strip()
        with self._lock:
            if key and key in self._devices:
                self._ui_device_id = key
            return self._ui_device_id

    def ui_device_id(self) -> str:
        return self.default_device_id()

    def status(self) -> dict:
        with self._lock:
            return {
                "ui_device_id": self._ui_device_id if self._devices else LEGACY_DEVICE_ID,
                "devices": [
                    {
                        "device_id": d.device_id,
                        "display_name": d.display_name or d.device_id,
                        "camera_url": d.effective_camera_url(),
                        "play_wav_url": d.effective_play_wav_url(),
                        "base_url": d.effective_base_url(),
                    }
                    for d in self._devices.values()
                ],
            }


_REGISTRY: DeviceRegistry | None = None


def get_device_registry() -> DeviceRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = DeviceRegistry()
    return _REGISTRY


def resolve_device_id(raw: str | None) -> str:
    """Normalize a client-provided device_id; empty → registry default."""
    reg = get_device_registry()
    cleaned = (raw or "").strip()
    if not cleaned:
        return reg.default_device_id()
    return reg.resolve_or_default(cleaned).device_id
