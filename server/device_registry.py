"""Multi-robot device registry — maps device_id → camera / playback URLs."""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from user_devices import format_device_mac, normalize_device_mac

try:
    from camera import effective_camera_rotation
except ImportError:
    def effective_camera_rotation(device_rotation: str | None = None) -> str:  # type: ignore[misc]
        return str(device_rotation or "none").strip() or "none"

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
    camera_rotation: str = "none"
    latitude: float | None = None
    longitude: float | None = None
    location_name: str = ""
    location_updated_at: str = ""
    wifi_ssid: str = ""
    wifi_bssid: str = ""
    wifi_rssi: int | None = None
    wifi_channel: int | None = None
    wifi_updated_at: str = ""

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
        # Tracks records loaded from / written to devices.json. This lets us
        # discard the in-memory legacy fallback once real LAN devices appear,
        # without accidentally deleting a manually configured "default" entry.
        self._persisted_device_ids: set[str] = set()
        self._ui_device_id: str = LEGACY_DEVICE_ID
        # Clients may repeatedly send a stale device ID while reconnecting.
        # Warn once per unknown ID so the fallback remains visible without flooding logs.
        # "default" is an alias for the UI device, not an unknown robot.
        self._warned_unknown_device_ids: set[str] = set()
        self.reload()

    def reload(self) -> None:
        file_records, dirty = self._load_from_file()
        persisted_ids = {d.device_id for d in file_records}
        loaded = file_records
        if not loaded:
            loaded = self._legacy_from_environ()
            if loaded:
                logger.info(
                    "Device registry empty — using legacy CAMERA_SOURCE / ESP_PLAY_WAV_URL "
                    "as device_id=%s",
                    LEGACY_DEVICE_ID,
                )
        with self._lock:
            if dirty:
                logger.info(
                    "Dropping non-MAC device registry entries; keeping %s",
                    list(persisted_ids) or ["(none)"],
                )
                self._write_devices_locked(file_records)
            self._devices = {d.device_id: d for d in loaded}
            self._persisted_device_ids = persisted_ids
            if self._ui_device_id not in self._devices and self._devices:
                self._ui_device_id = next(iter(self._devices))
            elif not self._devices:
                self._ui_device_id = LEGACY_DEVICE_ID
        logger.info(
            "Device registry loaded: %d device(s) %s",
            len(self._devices),
            list(self._devices.keys()),
        )

    def upsert_discovered(self, devices: list[DeviceRecord]) -> list[DeviceRecord]:
        """Persist discovered LAN devices and update the live registry.

        Discovery owns network endpoint fields because DHCP may change them.
        An existing display name is retained as a simple manual override.
        Returns the records that were added or changed.
        """
        cleaned = self._clean_discovered(devices)
        if not cleaned:
            return []

        with self._lock:
            merged = dict(self._devices)
            removed = False
            # The default entry synthesized from legacy environment variables
            # must not become a permanent device merely because discovery ran.
            if LEGACY_DEVICE_ID not in self._persisted_device_ids:
                removed = merged.pop(LEGACY_DEVICE_ID, None) is not None

            changed: list[DeviceRecord] = []
            for device_id, discovered in cleaned.items():
                existing = merged.get(device_id)
                record = self._merge_discovered_record(existing, discovered)
                if existing != record:
                    merged[device_id] = record
                    changed.append(record)

            if not changed and not removed:
                return []

            self._write_devices_locked(merged.values())
            self._devices = merged
            self._persisted_device_ids = set(merged)
            if self._ui_device_id not in self._devices:
                self._ui_device_id = next(iter(self._devices), LEGACY_DEVICE_ID)
            return changed

    def replace_with_discovered(self, devices: list[DeviceRecord]) -> list[DeviceRecord]:
        """Replace persisted devices with those confirmed by the current LAN scan.

        Used at server startup so offline devices from a prior server run do not
        appear in the UI. Per-device settings are retained when the same device
        is found again. An empty scan keeps the persisted inventory — mDNS/UDP
        often misses robots on the first boot pass.
        """
        cleaned = self._clean_discovered(devices)
        if not cleaned:
            with self._lock:
                kept = len(self._devices)
            if kept:
                logger.info(
                    "Startup discovery found no robots — keeping %d persisted device(s)",
                    kept,
                )
            return []

        with self._lock:
            replacement = {
                device_id: self._merge_discovered_record(
                    self._devices.get(device_id) or self._find_locked(device_id),
                    discovered,
                )
                for device_id, discovered in cleaned.items()
            }
            changed = [
                record
                for device_id, record in replacement.items()
                if self._devices.get(device_id) != record
            ]
            removed = set(self._devices) - set(replacement)
            if not changed and not removed:
                return []

            self._write_devices_locked(replacement.values())
            self._devices = replacement
            self._persisted_device_ids = set(replacement)
            if self._ui_device_id not in self._devices:
                self._ui_device_id = next(iter(self._devices), LEGACY_DEVICE_ID)
            return changed

    @staticmethod
    def _clean_discovered(devices: list[DeviceRecord]) -> dict[str, DeviceRecord]:
        cleaned: dict[str, DeviceRecord] = {}
        for device in devices:
            mac = normalize_device_mac(device.device_id)
            if not mac or not device.effective_base_url():
                continue
            cleaned[mac] = DeviceRecord(
                device_id=mac,
                display_name=device.display_name.strip() or format_device_mac(mac),
                camera_url=device.camera_url,
                play_wav_url=device.play_wav_url,
                base_url=device.base_url,
                camera_rotation=device.camera_rotation,
                latitude=device.latitude,
                longitude=device.longitude,
                location_name=device.location_name,
                location_updated_at=device.location_updated_at,
                wifi_ssid=device.wifi_ssid,
                wifi_bssid=device.wifi_bssid,
                wifi_rssi=device.wifi_rssi,
                wifi_channel=device.wifi_channel,
                wifi_updated_at=device.wifi_updated_at,
            )
        return cleaned

    @staticmethod
    def _merge_discovered_record(
        existing: DeviceRecord | None, discovered: DeviceRecord
    ) -> DeviceRecord:
        return DeviceRecord(
            device_id=discovered.device_id.strip(),
            display_name=(
                existing.display_name.strip()
                if existing and existing.display_name.strip()
                else discovered.display_name.strip()
            ),
            camera_url=discovered.effective_camera_url(),
            play_wav_url=discovered.effective_play_wav_url(),
            base_url=discovered.effective_base_url(),
            camera_rotation=(
                effective_camera_rotation(existing.camera_rotation)
                if existing
                else effective_camera_rotation(None)
            ),
            latitude=existing.latitude if existing else None,
            longitude=existing.longitude if existing else None,
            location_name=existing.location_name if existing else "",
            location_updated_at=existing.location_updated_at if existing else "",
            wifi_ssid=existing.wifi_ssid if existing else "",
            wifi_bssid=existing.wifi_bssid if existing else "",
            wifi_rssi=existing.wifi_rssi if existing else None,
            wifi_channel=existing.wifi_channel if existing else None,
            wifi_updated_at=existing.wifi_updated_at if existing else "",
        )

    def set_camera_rotation(self, device_id: str, rotation: str) -> DeviceRecord:
        """Persist a validated per-device camera orientation."""
        key = (device_id or "").strip()
        if key.casefold() in {"", "default", "ui"}:
            key = self.ui_device_id()
        with self._lock:
            existing = self._find_locked(key)
            if existing is None:
                raise KeyError(key)
            record = DeviceRecord(
                device_id=existing.device_id,
                display_name=existing.display_name,
                camera_url=existing.camera_url,
                play_wav_url=existing.play_wav_url,
                base_url=existing.base_url,
                camera_rotation=rotation,
                latitude=existing.latitude,
                longitude=existing.longitude,
                location_name=existing.location_name,
                location_updated_at=existing.location_updated_at,
                wifi_ssid=existing.wifi_ssid,
                wifi_bssid=existing.wifi_bssid,
                wifi_rssi=existing.wifi_rssi,
                wifi_channel=existing.wifi_channel,
                wifi_updated_at=existing.wifi_updated_at,
            )
            if existing == record:
                return record
            updated = dict(self._devices)
            updated[existing.device_id] = record
            self._write_devices_locked(updated.values())
            self._devices = updated
            self._persisted_device_ids = set(updated)
            return record

    def set_location(
        self,
        device_id: str,
        *,
        latitude: float,
        longitude: float,
        location_name: str | None = None,
    ) -> DeviceRecord:
        """Persist a GPS/location fix supplied by a known device."""
        if not -90.0 <= latitude <= 90.0:
            raise ValueError("Latitude must be between -90 and 90")
        if not -180.0 <= longitude <= 180.0:
            raise ValueError("Longitude must be between -180 and 180")
        if not math.isfinite(latitude) or not math.isfinite(longitude):
            raise ValueError("Location coordinates must be finite numbers")
        if location_name is not None and len(location_name.strip()) > 100:
            raise ValueError("Location name must contain at most 100 characters")

        key = (device_id or "").strip()
        with self._lock:
            existing = self._find_locked(key)
            if existing is None:
                raise KeyError(key)
            record = DeviceRecord(
                device_id=existing.device_id,
                display_name=existing.display_name,
                camera_url=existing.camera_url,
                play_wav_url=existing.play_wav_url,
                base_url=existing.base_url,
                camera_rotation=existing.camera_rotation,
                latitude=latitude,
                longitude=longitude,
                location_name=(
                    location_name.strip()
                    if location_name is not None
                    else existing.location_name
                ),
                location_updated_at=datetime.now(timezone.utc).isoformat(),
                wifi_ssid=existing.wifi_ssid,
                wifi_bssid=existing.wifi_bssid,
                wifi_rssi=existing.wifi_rssi,
                wifi_channel=existing.wifi_channel,
                wifi_updated_at=existing.wifi_updated_at,
            )
            updated = dict(self._devices)
            updated[existing.device_id] = record
            self._write_devices_locked(updated.values())
            self._devices = updated
            self._persisted_device_ids = set(updated)
            return record

    def set_wifi_network(
        self,
        device_id: str,
        *,
        ssid: str,
        bssid: str,
        rssi: int | None = None,
        channel: int | None = None,
    ) -> DeviceRecord:
        """Persist the Wi-Fi network most recently reported by a known device."""
        normalized_ssid = ssid.strip()
        normalized_bssid = bssid.strip().upper()
        if not normalized_ssid or len(normalized_ssid) > 32:
            raise ValueError("Wi-Fi SSID must contain 1 to 32 characters")
        if not _is_valid_bssid(normalized_bssid):
            raise ValueError("Wi-Fi BSSID must be a MAC address such as AA:BB:CC:DD:EE:FF")
        if rssi is not None and not -127 <= rssi <= 0:
            raise ValueError("Wi-Fi RSSI must be between -127 and 0 dBm")
        if channel is not None and not 1 <= channel <= 196:
            raise ValueError("Wi-Fi channel must be between 1 and 196")

        key = (device_id or "").strip()
        with self._lock:
            existing = self._find_locked(key)
            if existing is None:
                raise KeyError(key)
            record = DeviceRecord(
                device_id=existing.device_id,
                display_name=existing.display_name,
                camera_url=existing.camera_url,
                play_wav_url=existing.play_wav_url,
                base_url=existing.base_url,
                camera_rotation=existing.camera_rotation,
                latitude=existing.latitude,
                longitude=existing.longitude,
                location_name=existing.location_name,
                location_updated_at=existing.location_updated_at,
                wifi_ssid=normalized_ssid,
                wifi_bssid=normalized_bssid,
                wifi_rssi=rssi,
                wifi_channel=channel,
                wifi_updated_at=datetime.now(timezone.utc).isoformat(),
            )
            updated = dict(self._devices)
            updated[existing.device_id] = record
            self._write_devices_locked(updated.values())
            self._devices = updated
            self._persisted_device_ids = set(updated)
            return record

    def _write_devices_locked(self, devices: object) -> None:
        """Atomically write registry records. Caller must hold ``_lock``."""
        records = sorted(devices, key=lambda d: d.device_id)
        payload = {
            "devices": [
                {
                    "device_id": d.device_id,
                    "display_name": d.display_name,
                    "camera_url": d.camera_url,
                    "play_wav_url": d.play_wav_url,
                    "base_url": d.base_url,
                    "camera_rotation": d.camera_rotation,
                    "latitude": d.latitude,
                    "longitude": d.longitude,
                    "location_name": d.location_name,
                    "location_updated_at": d.location_updated_at,
                    "wifi_ssid": d.wifi_ssid,
                    "wifi_bssid": d.wifi_bssid,
                    "wifi_rssi": d.wifi_rssi,
                    "wifi_channel": d.wifi_channel,
                    "wifi_updated_at": d.wifi_updated_at,
                }
                for d in records
            ]
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(f"{self._path.suffix}.tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self._path)

    def _load_from_file(self) -> tuple[list[DeviceRecord], bool]:
        if not self._path.is_file():
            return [], False
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Could not read %s: %s", self._path, exc)
            return [], False
        items = raw.get("devices", raw) if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            return [], False
        out: list[DeviceRecord] = []
        dirty = False
        for item in items:
            if not isinstance(item, dict):
                dirty = True
                continue
            raw_id = str(item.get("device_id") or item.get("mac") or "").strip()
            device_id = normalize_device_mac(raw_id)
            if not device_id:
                logger.warning("Dropping non-MAC device registry entry id=%r", raw_id)
                dirty = True
                continue
            if device_id != raw_id:
                dirty = True
            out.append(
                DeviceRecord(
                    device_id=device_id,
                    display_name=str(item.get("display_name") or "").strip(),
                    camera_url=str(item.get("camera_url") or "").strip(),
                    play_wav_url=str(item.get("play_wav_url") or "").strip(),
                    base_url=str(item.get("base_url") or "").strip(),
                    camera_rotation=effective_camera_rotation(
                        str(item.get("camera_rotation") or "").strip() or None
                    ),
                    latitude=_parse_coordinate(item.get("latitude"), -90.0, 90.0),
                    longitude=_parse_coordinate(item.get("longitude"), -180.0, 180.0),
                    location_name=_parse_location_name(item.get("location_name")),
                    location_updated_at=str(
                        item.get("location_updated_at") or ""
                    ).strip(),
                    wifi_ssid=_parse_wifi_ssid(item.get("wifi_ssid")),
                    wifi_bssid=_parse_bssid(item.get("wifi_bssid")),
                    wifi_rssi=_parse_optional_int(item.get("wifi_rssi"), -127, 0),
                    wifi_channel=_parse_optional_int(item.get("wifi_channel"), 1, 196),
                    wifi_updated_at=str(item.get("wifi_updated_at") or "").strip(),
                )
            )
        return out, dirty

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
                camera_rotation=effective_camera_rotation(None),
                latitude=None,
                longitude=None,
                location_name="",
            )
        ]

    def remove_devices(self, device_ids: list[str]) -> list[str]:
        """Drop robots that discovery no longer sees on the LAN."""
        wanted = {str(device_id).strip() for device_id in device_ids if str(device_id).strip()}
        if not wanted:
            return []
        with self._lock:
            removed = [device_id for device_id in wanted if device_id in self._devices]
            if not removed:
                return []
            updated = {
                device_id: record
                for device_id, record in self._devices.items()
                if device_id not in wanted
            }
            self._write_devices_locked(updated.values())
            self._devices = updated
            self._persisted_device_ids = set(updated)
            if self._ui_device_id not in self._devices:
                self._ui_device_id = next(iter(self._devices), LEGACY_DEVICE_ID)
            logger.info("Device registry removed %d device(s): %s", len(removed), removed)
            return removed

    def list_devices(self) -> list[DeviceRecord]:
        with self._lock:
            return list(self._devices.values())

    def find_device_id_by_host(self, host: str) -> str:
        """Map a voice-WS client IP to a discovered robot MAC."""
        needle = (host or "").strip()
        if not needle:
            return ""
        with self._lock:
            devices = list(self._devices.values())
        for record in devices:
            parsed = urllib.parse.urlparse(record.effective_base_url())
            if parsed.hostname == needle:
                return record.device_id
        return ""

    def ensure_from_client_host(self, host: str, *, display_name: str = "") -> tuple[str, bool]:
        """Resolve or register a robot by LAN IP. Returns (mac, created)."""
        needle = (host or "").strip()
        if not needle:
            return "", False
        mapped = self.find_device_id_by_host(needle)
        if mapped:
            return mapped, False
        from device_discovery import lookup_lan_mac

        mac = lookup_lan_mac(needle)
        if not mac:
            return "", False
        found = self.get(mac)
        if found is not None:
            return found.device_id, False
        record = self.ensure_registered(
            mac,
            display_name=display_name,
            base_url=f"http://{needle}",
        )
        return record.device_id, True

    def _find_locked(self, device_id: str) -> DeviceRecord | None:
        """Exact match, then case-insensitive. Caller must hold ``self._lock``."""
        key = (device_id or "").strip()
        if not key:
            return None
        found = self._devices.get(key)
        if found is not None:
            return found
        folded = key.casefold()
        for stored_id, record in self._devices.items():
            if stored_id.casefold() == folded:
                return record
        mac = normalize_device_mac(key)
        if mac and mac != key:
            found = self._devices.get(mac)
            if found is not None:
                return found
            for stored_id, record in self._devices.items():
                if normalize_device_mac(stored_id) == mac:
                    return record
        return None

    def get(self, device_id: str | None) -> DeviceRecord | None:
        key = (device_id or "").strip()
        if not key:
            return None
        with self._lock:
            return self._find_locked(key)

    def default_device_id(self) -> str:
        with self._lock:
            if self._ui_device_id in self._devices:
                return self._ui_device_id
            if self._devices:
                return next(iter(self._devices))
            return LEGACY_DEVICE_ID

    def resolve_or_default(self, device_id: str | None) -> DeviceRecord:
        """Return the MAC device, or the UI device for empty/default/ui.

        Never remaps a name or unknown id onto another robot.
        """
        with self._lock:
            key = (device_id or "").strip()
            alias = key.casefold() in {"", "default", "ui"}
            if alias:
                fallback_id = self.default_device_id()
                fallback = self._find_locked(fallback_id)
                if fallback is not None:
                    return fallback
            else:
                mac = normalize_device_mac(key)
                if mac:
                    found = self._find_locked(mac)
                    if found is not None:
                        return found
                    if mac not in self._warned_unknown_device_ids:
                        logger.warning("Unknown device mac=%s — not remapped", mac)
                        self._warned_unknown_device_ids.add(mac)
                    return DeviceRecord(device_id=mac)
                if key not in self._warned_unknown_device_ids:
                    logger.warning(
                        "Rejected non-MAC device_id=%r — not remapped",
                        key,
                    )
                    self._warned_unknown_device_ids.add(key)
                return DeviceRecord(device_id="")
        legacy = self._legacy_from_environ()
        return legacy[0]

    def ensure_registered(
        self,
        device_id: str,
        *,
        display_name: str = "",
        base_url: str = "",
    ) -> DeviceRecord:
        """Create or return this MAC. Names are rejected; never remapped."""
        key = normalize_device_mac(device_id)
        if not key:
            raise ValueError(f"device_id must be a MAC, got {device_id!r}")
        with self._lock:
            found = self._find_locked(key)
            if found is not None:
                return found
            pretty = format_device_mac(key)
            record = DeviceRecord(
                device_id=key,
                display_name=(display_name or pretty or key).strip(),
                base_url=(base_url or "").strip(),
                camera_url=f"{base_url.rstrip('/')}/stream" if base_url.strip() else "",
                play_wav_url=f"{base_url.rstrip('/')}/play_wav" if base_url.strip() else "",
            )
            updated = dict(self._devices)
            updated[key] = record
            self._write_devices_locked(updated.values())
            self._devices = updated
            self._persisted_device_ids = set(updated)
            logger.info(
                "Registered device mac=%s id=%s",
                pretty or key,
                key,
            )
            return record

    def set_ui_device_id(self, device_id: str) -> str:
        key = normalize_device_mac(device_id) or (device_id or "").strip()
        with self._lock:
            found = self._find_locked(key) if key else None
            if found is not None:
                self._ui_device_id = found.device_id
            return self._ui_device_id

    def ui_device_id(self) -> str:
        return self.default_device_id()

    def status(self) -> dict:
        with self._lock:
            devices = list(self._devices.values())
            return {
                "ui_device_id": self._ui_device_id if self._devices else LEGACY_DEVICE_ID,
                "count": len(devices),
                "devices": [
                    {
                        "device_id": d.device_id,
                        "display_name": d.display_name or d.device_id,
                        "camera_url": d.effective_camera_url(),
                        "play_wav_url": d.effective_play_wav_url(),
                        "base_url": d.effective_base_url(),
                        "camera_rotation": d.camera_rotation,
                        "latitude": d.latitude,
                        "longitude": d.longitude,
                        "location_name": d.location_name,
                        "location_updated_at": d.location_updated_at,
                        "wifi_ssid": d.wifi_ssid,
                        "wifi_bssid": d.wifi_bssid,
                        "wifi_rssi": d.wifi_rssi,
                        "wifi_channel": d.wifi_channel,
                        "wifi_updated_at": d.wifi_updated_at,
                    }
                    for d in devices
                ],
            }


_REGISTRY: DeviceRegistry | None = None


def get_device_registry() -> DeviceRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = DeviceRegistry()
    return _REGISTRY


def resolve_device_id(raw: str | None) -> str:
    """Normalize a client MAC. Names are rejected; unknown MACs are not persisted.

    Discovery is the only path that adds robots. HTTP/camera callers that still
    send a powered-off MAC must not resurrect it in devices.json.
    """
    reg = get_device_registry()
    cleaned = (raw or "").strip()
    if not cleaned or cleaned.casefold() in {"default", "ui"}:
        return reg.default_device_id()
    mac = normalize_device_mac(cleaned)
    if not mac:
        logger.warning("Rejected non-MAC device_id=%r", cleaned)
        return ""
    found = reg.get(mac)
    if found is not None:
        return found.device_id
    logger.warning("Unknown device mac=%s — not remapped", mac)
    return ""


def _parse_coordinate(value: object, minimum: float, maximum: float) -> float | None:
    """Read an optional finite coordinate from manually edited device JSON."""
    try:
        coordinate = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(coordinate) or not minimum <= coordinate <= maximum:
        return None
    return coordinate


def _parse_location_name(value: object) -> str:
    name = str(value or "").strip()
    return name if len(name) <= 100 else ""


def _parse_wifi_ssid(value: object) -> str:
    ssid = str(value or "").strip()
    return ssid if 0 < len(ssid) <= 32 else ""


def _is_valid_bssid(value: str) -> bool:
    parts = value.split(":")
    if len(parts) != 6:
        return False
    try:
        return all(len(part) == 2 and 0 <= int(part, 16) <= 255 for part in parts)
    except ValueError:
        return False


def _parse_bssid(value: object) -> str:
    bssid = str(value or "").strip().upper()
    return bssid if _is_valid_bssid(bssid) else ""


def _parse_optional_int(value: object, minimum: int, maximum: int) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if minimum <= number <= maximum else None
