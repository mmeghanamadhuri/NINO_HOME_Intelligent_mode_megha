"""LAN-only NiNO ESP discovery via mDNS and firmware UDP discovery."""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone

import requests

from device_registry import DeviceRecord, DeviceRegistry, get_device_registry

logger = logging.getLogger(__name__)

MDNS_SERVICE_TYPE = "_nino._tcp.local."
DISCOVERY_MULTICAST = ("239.255.255.250", 1900)
DISCOVERY_BROADCAST = ("255.255.255.255", 1900)


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name, str(int(default))).strip().lower()
    return value not in {"0", "false", "no", "off"}


def _env_float(name: str, default: float, minimum: float = 0.1) -> float:
    try:
        return max(minimum, float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def _ipv4(value: object) -> str | None:
    try:
        address = ipaddress.ip_address(str(value).strip())
    except ValueError:
        return None
    return str(address) if address.version == 4 else None


class _MdnsListener:
    def __init__(self) -> None:
        self.names: set[str] = set()
        self.lock = threading.Lock()

    def add_service(self, zeroconf, service_type: str, name: str) -> None:
        with self.lock:
            self.names.add(name)

    def update_service(self, zeroconf, service_type: str, name: str) -> None:
        self.add_service(zeroconf, service_type, name)

    def remove_service(self, zeroconf, service_type: str, name: str) -> None:
        return None


class DeviceDiscovery:
    """Periodically discover NiNO robots and refresh the live registry."""

    def __init__(
        self,
        registry: DeviceRegistry | None = None,
        on_registry_updated: Callable[[], None] | None = None,
    ) -> None:
        self.registry = registry or get_device_registry()
        self.on_registry_updated = on_registry_updated
        self.enabled = _env_bool("NINO_DISCOVERY_ENABLED", True)
        self.interval_s = _env_float("NINO_DISCOVERY_INTERVAL_S", 8.0, 2.0)
        self.stale_after_s = _env_float(
            "NINO_DISCOVERY_STALE_S", max(16.0, self.interval_s * 2.5), 4.0
        )
        self.http_timeout_s = _env_float("NINO_DISCOVERY_HTTP_TIMEOUT_S", 1.5)
        self.mdns_timeout_s = _env_float("NINO_DISCOVERY_MDNS_TIMEOUT_S", 1.5)
        self.udp_wait_s = _env_float("NINO_DISCOVERY_UDP_WAIT_S", 1.0)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._scan_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._last_seen: dict[str, float] = {}
        self._last_scan_at: str | None = None
        self._last_duration_ms: int | None = None
        self._last_found = 0
        self._last_updated = 0
        self._last_error: str | None = None

    def status(self) -> dict:
        with self._state_lock:
            return {
                "enabled": self.enabled,
                "running": bool(self._thread and self._thread.is_alive()),
                "interval_s": self.interval_s,
                "stale_after_s": self.stale_after_s,
                "last_scan_at": self._last_scan_at,
                "last_duration_ms": self._last_duration_ms,
                "last_found": self._last_found,
                "last_updated": self._last_updated,
                "last_error": self._last_error,
            }

    def start(self) -> None:
        if not self.enabled or (self._thread and self._thread.is_alive()):
            return
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="nino-device-discovery",
        )
        self._thread.start()
        logger.info(
            "NiNO discovery enabled: mDNS %s, UDP %s, every %.0fs (stale after %.0fs)",
            MDNS_SERVICE_TYPE,
            f"{DISCOVERY_MULTICAST[0]}:{DISCOVERY_MULTICAST[1]}",
            self.interval_s,
            self.stale_after_s,
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=min(2.0, self.http_timeout_s + self.mdns_timeout_s))

    def _run(self) -> None:
        while not self._stop.is_set():
            self.discover_once()
            self._stop.wait(self.interval_s)

    def discover_once(self, *, replace_registry: bool = False) -> list[DeviceRecord]:
        """Scan mDNS/UDP, query each candidate's ``/status``, then update the registry.

        ``replace_registry`` is used only at server startup to remove devices
        that were persisted during a previous run but are no longer reachable.
        """
        if not self.enabled:
            return []
        if not self._scan_lock.acquire(blocking=False):
            logger.debug("NiNO discovery scan already running")
            return []
        started = time.monotonic()
        try:
            candidates = self._discover_mdns()
            candidates.update(self._discover_udp())
            records = self._records_from_status(candidates)
            changed = (
                self.registry.replace_with_discovered(records)
                if replace_registry
                else self.registry.upsert_discovered(records)
            )
            removed: list[str] = []
            if replace_registry:
                self._last_seen = {
                    record.device_id: time.monotonic() for record in records
                }
            else:
                removed = self._prune_stale(records)
            live = self.registry.list_devices()
            logger.info(
                "DEVICES live=%d found=%d updated=%d removed=%d ids=%s",
                len(live),
                len(records),
                len(changed),
                len(removed),
                [record.device_id for record in live] or ["(none)"],
            )
            if changed or removed:
                logger.info(
                    "NiNO discovery updated %d / removed %d device(s)",
                    len(changed),
                    len(removed),
                )
                if self.on_registry_updated:
                    try:
                        self.on_registry_updated()
                    except Exception:
                        logger.exception("Could not refresh cameras after NiNO discovery")
            elif records:
                logger.debug("NiNO discovery found %d unchanged device(s)", len(records))

            self._set_scan_state(
                found=len(records),
                updated=len(changed) + len(removed),
                error=None,
                started=started,
            )
            return records
        except Exception as exc:
            # Discovery is optional infrastructure: never let a LAN failure
            # affect the FastAPI process or the legacy single-device fallback.
            logger.warning("NiNO discovery scan failed: %s", exc)
            self._set_scan_state(found=0, updated=0, error=str(exc), started=started)
            return []
        finally:
            self._scan_lock.release()

    def _prune_stale(self, found: list[DeviceRecord]) -> list[str]:
        now = time.monotonic()
        found_ids = {record.device_id for record in found}
        for device_id in found_ids:
            self._last_seen[device_id] = now
        stale: list[str] = []
        for record in self.registry.list_devices():
            device_id = record.device_id
            last_seen = self._last_seen.get(device_id)
            if last_seen is None:
                self._last_seen[device_id] = now
                continue
            if device_id not in found_ids and (now - last_seen) >= self.stale_after_s:
                stale.append(device_id)
        if not stale:
            return []
        removed = self.registry.remove_devices(stale)
        for device_id in removed:
            self._last_seen.pop(device_id, None)
        return removed

    def _set_scan_state(
        self, *, found: int, updated: int, error: str | None, started: float
    ) -> None:
        with self._state_lock:
            self._last_scan_at = datetime.now(timezone.utc).isoformat()
            self._last_duration_ms = round((time.monotonic() - started) * 1000)
            self._last_found = found
            self._last_updated = updated
            self._last_error = error

    def _discover_mdns(self) -> dict[tuple[str, int], None]:
        try:
            from zeroconf import IPVersion, ServiceBrowser, Zeroconf
        except ImportError:
            logger.debug("zeroconf unavailable; using NiNO UDP discovery only")
            return {}

        addresses: dict[tuple[str, int], None] = {}
        zeroconf = Zeroconf()
        listener = _MdnsListener()
        browser = None
        try:
            browser = ServiceBrowser(zeroconf, MDNS_SERVICE_TYPE, listener)
            time.sleep(self.mdns_timeout_s)
            with listener.lock:
                names = list(listener.names)
            for name in names:
                info = zeroconf.get_service_info(
                    MDNS_SERVICE_TYPE, name, timeout=int(self.mdns_timeout_s * 1000)
                )
                if info is None:
                    continue
                port = info.port or 80
                for raw_ip in info.parsed_addresses(IPVersion=IPVersion.V4):
                    ip = _ipv4(raw_ip)
                    if ip:
                        addresses[(ip, port)] = None
        except Exception as exc:
            logger.debug("NiNO mDNS browse failed: %s", exc)
        finally:
            if browser is not None:
                browser.cancel()
            zeroconf.close()
        return addresses

    def _discover_udp(self) -> dict[tuple[str, int], None]:
        addresses: dict[tuple[str, int], None] = {}
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
                sock.settimeout(0.15)
                for target in (DISCOVERY_MULTICAST, DISCOVERY_BROADCAST):
                    try:
                        sock.sendto(b"discover", target)
                    except OSError as exc:
                        logger.debug("NiNO UDP discovery send to %s failed: %s", target, exc)
                deadline = time.monotonic() + self.udp_wait_s
                while time.monotonic() < deadline:
                    try:
                        _payload, sender = sock.recvfrom(512)
                    except TimeoutError:
                        continue
                    ip = _ipv4(sender[0])
                    if ip:
                        # Firmware replies from its discovery listener; HTTP
                        # remains on its standard web server port.
                        addresses[(ip, 80)] = None
        except OSError as exc:
            logger.debug("NiNO UDP discovery unavailable: %s", exc)
        return addresses

    def _records_from_status(
        self, candidates: dict[tuple[str, int], None]
    ) -> list[DeviceRecord]:
        records: dict[str, DeviceRecord] = {}
        duplicate_ids: dict[str, set[str]] = {}
        for ip, port in candidates:
            base_url = f"http://{ip}" if port == 80 else f"http://{ip}:{port}"
            try:
                response = requests.get(
                    f"{base_url}/status", timeout=self.http_timeout_s
                )
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as exc:
                logger.debug("NiNO status query failed for %s: %s", base_url, exc)
                continue
            if not isinstance(payload, dict):
                continue
            device_id = str(payload.get("device_id") or "").strip()
            if not device_id:
                logger.debug("NiNO status at %s did not include device_id", base_url)
                continue
            # Firmware reports the currently assigned address too. Prefer it
            # for persisted URLs when valid; the request itself always goes to
            # the mDNS/UDP-discovered endpoint above.
            reported_ip = _ipv4(payload.get("ip")) or ip
            record_base_url = (
                f"http://{reported_ip}"
                if port == 80
                else f"http://{reported_ip}:{port}"
            )
            display_name = str(
                payload.get("device_name") or payload.get("name") or device_id
            ).strip()
            record = DeviceRecord(
                device_id=device_id,
                display_name=display_name,
                base_url=record_base_url,
                camera_url=f"{record_base_url}/stream",
                play_wav_url=f"{record_base_url}/play_wav",
            )
            existing = records.get(device_id)
            if existing and existing.effective_base_url() != record.effective_base_url():
                duplicate_ids.setdefault(
                    device_id,
                    {existing.effective_base_url()},
                ).add(record.effective_base_url())
                continue
            records[device_id] = record

        for device_id, endpoints in duplicate_ids.items():
            # A device_id is the routing key for voice, camera, playback and
            # alarms. Keep neither discovery result rather than letting the
            # selected robot flip nondeterministically between two endpoints.
            records.pop(device_id, None)
            logger.error(
                "Duplicate NiNO device_id=%r reported by %s; assign each robot "
                "a unique device_id before it can be routed safely",
                device_id,
                ", ".join(sorted(endpoints)),
            )
        return list(records.values())


_discovery: DeviceDiscovery | None = None


def start_discovery_loop(
    on_registry_updated: Callable[[], None] | None = None,
) -> DeviceDiscovery:
    global _discovery
    if _discovery is None:
        _discovery = DeviceDiscovery(on_registry_updated=on_registry_updated)
    elif on_registry_updated is not None:
        _discovery.on_registry_updated = on_registry_updated
    _discovery.start()
    return _discovery


def stop_discovery_loop() -> None:
    if _discovery is not None:
        _discovery.stop()


def discover_once(*, replace_registry: bool = False) -> list[DeviceRecord]:
    """Run an immediate discovery scan.

    Set ``replace_registry`` during startup so only currently reachable LAN
    devices remain in ``devices.json``.
    """
    service = _discovery or DeviceDiscovery()
    return service.discover_once(replace_registry=replace_registry)


def discovery_status() -> dict:
    if _discovery is None:
        return {"enabled": _env_bool("NINO_DISCOVERY_ENABLED", True), "running": False}
    return _discovery.status()
