"""Store firmware images and push OTA updates to robots by MAC / device_id."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from network_util import public_http_base as lan_http_base
from user_devices import normalize_device_mac

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_FIRMWARE_DIR = BASE_DIR / "data" / "firmware"
INDEX_NAME = "index.json"
MAX_FIRMWARE_BYTES = int(os.environ.get("NINO_OTA_MAX_BYTES", str(12 * 1024 * 1024)))
ESP_IMAGE_MAGIC = 0xE9


@dataclass(frozen=True)
class FirmwareRecord:
    firmware_id: str
    filename: str
    sha256: str
    size: int
    created_at: str
    label: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class OtaError(Exception):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def public_http_base() -> str:
    """LAN URL the robot can use to pull firmware from this server."""
    base = lan_http_base()
    if not base:
        raise OtaError("Set NINO_SERVER_LAN_HOST so robots can download firmware", 503)
    return base


def ota_token() -> str:
    return os.environ.get("NINO_OTA_TOKEN", "").strip()


def firmware_dir() -> Path:
    raw = os.environ.get("NINO_OTA_DIR", "").strip()
    path = Path(raw) if raw else DEFAULT_FIRMWARE_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def looks_like_esp_image(data: bytes) -> bool:
    return len(data) >= 8 and data[0] == ESP_IMAGE_MAGIC


class OtaService:
    def __init__(self, directory: Path | None = None) -> None:
        self._dir = directory or firmware_dir()
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _index_path(self) -> Path:
        return self._dir / INDEX_NAME

    def _load_index(self) -> list[FirmwareRecord]:
        path = self._index_path()
        if not path.is_file():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("Could not read %s", path)
            return []
        items = raw.get("firmware") if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            return []
        out: list[FirmwareRecord] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                out.append(
                    FirmwareRecord(
                        firmware_id=str(item["firmware_id"]),
                        filename=str(item.get("filename") or ""),
                        sha256=str(item["sha256"]),
                        size=int(item["size"]),
                        created_at=str(item.get("created_at") or ""),
                        label=str(item.get("label") or ""),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def _write_index(self, records: list[FirmwareRecord]) -> None:
        path = self._index_path()
        tmp = path.with_suffix(".json.tmp")
        payload = {"firmware": [r.as_dict() for r in records]}
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)

    def list_firmware(self) -> list[FirmwareRecord]:
        with self._lock:
            return list(self._load_index())

    def get(self, firmware_id: str) -> FirmwareRecord | None:
        wanted = str(firmware_id or "").strip()
        if not wanted:
            return None
        for record in self.list_firmware():
            if record.firmware_id == wanted or record.sha256 == wanted:
                return record
        return None

    def bin_path(self, record: FirmwareRecord) -> Path:
        return self._dir / f"{record.firmware_id}.bin"

    def save_firmware(
        self,
        data: bytes,
        filename: str = "",
        label: str = "",
    ) -> FirmwareRecord:
        if not data:
            raise OtaError("Firmware file is empty")
        if len(data) > MAX_FIRMWARE_BYTES:
            raise OtaError(f"Firmware exceeds {MAX_FIRMWARE_BYTES} bytes")
        if not looks_like_esp_image(data):
            raise OtaError("File is not an ESP-IDF application image")
        digest = sha256_bytes(data)
        firmware_id = digest[:16]
        now = datetime.now(timezone.utc).isoformat()
        record = FirmwareRecord(
            firmware_id=firmware_id,
            filename=Path(filename or "firmware.bin").name,
            sha256=digest,
            size=len(data),
            created_at=now,
            label=str(label or "").strip(),
        )
        with self._lock:
            path = self.bin_path(record)
            path.write_bytes(data)
            records = [r for r in self._load_index() if r.firmware_id != firmware_id]
            records.insert(0, record)
            self._write_index(records)
        logger.info("Stored firmware id=%s sha256=%s size=%d", firmware_id, digest, len(data))
        return record

    def pull_url(self, firmware_id: str) -> str:
        record = self.get(firmware_id)
        if record is None:
            raise OtaError("Unknown firmware_id", 404)
        url = f"{public_http_base()}/api/ota/firmware/{record.firmware_id}/bin"
        token = ota_token()
        if token:
            url = f"{url}?token={token}"
        return url

    def trigger_device(
        self,
        device_id: str,
        *,
        firmware_id: str | None = None,
        url: str | None = None,
        timeout_s: float = 15.0,
    ) -> dict[str, Any]:
        mac = normalize_device_mac(device_id)
        if not mac:
            raise OtaError("device_id must be a 12-hex MAC")
        from esp_playback import device_base_url

        base = device_base_url(mac)
        if not base:
            raise OtaError(f"Device {mac} is not online (no base URL)", 404)

        payload: dict[str, Any]
        record: FirmwareRecord | None = None
        if firmware_id:
            record = self.get(firmware_id)
            if record is None:
                raise OtaError("Unknown firmware_id", 404)
            payload = {
                "url": self.pull_url(record.firmware_id),
                "sha256": record.sha256,
                "size": record.size,
            }
        elif url:
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise OtaError("url must be http(s)")
            payload = {"url": url}
        else:
            raise OtaError("Provide firmware_id or url")

        robot_url = f"{base.rstrip('/')}/ota"
        try:
            resp = requests.post(robot_url, json=payload, timeout=timeout_s)
        except requests.RequestException as exc:
            logger.warning("OTA trigger failed device=%s: %s", mac, exc)
            raise OtaError("Could not reach robot /ota", 502) from exc
        if resp.status_code >= 400:
            raise OtaError(f"Robot rejected OTA (HTTP {resp.status_code})", 502)
        body: Any
        try:
            body = resp.json()
        except ValueError:
            body = {"ok": resp.ok, "text": resp.text[:200]}
        return {
            "ok": True,
            "device_id": mac,
            "robot_url": robot_url,
            "firmware_id": record.firmware_id if record else None,
            "sha256": record.sha256 if record else None,
            "size": record.size if record else None,
            "pull_url": payload.get("url"),
            "robot": body,
        }

    def robot_status(self, device_id: str, timeout_s: float = 6.0) -> dict[str, Any]:
        mac = normalize_device_mac(device_id)
        if not mac:
            raise OtaError("device_id must be a 12-hex MAC")
        from esp_playback import device_base_url

        base = device_base_url(mac)
        if not base:
            raise OtaError(f"Device {mac} is not online (no base URL)", 404)
        url = f"{base.rstrip('/')}/ota/status"
        try:
            resp = requests.get(url, timeout=timeout_s)
        except requests.RequestException as exc:
            raise OtaError("Could not reach robot /ota/status", 502) from exc
        try:
            body = resp.json()
        except ValueError as exc:
            raise OtaError("Robot returned non-JSON OTA status", 502) from exc
        return {"ok": True, "device_id": mac, **body}


_service: OtaService | None = None
_service_lock = threading.Lock()


def get_ota_service() -> OtaService:
    global _service
    with _service_lock:
        if _service is None:
            _service = OtaService()
        return _service


def reset_ota_service_for_tests() -> None:
    global _service
    with _service_lock:
        _service = None
