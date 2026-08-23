"""Firmware OTA hosting and bot deployment from the NiNO server."""

from __future__ import annotations

import json
import logging
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

_FIRMWARE_DIR = Path(__file__).resolve().parent / "firmware"
_PENDING_PATH = Path(__file__).resolve().parent / "data" / "ota_pending.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def firmware_dir() -> Path:
    _FIRMWARE_DIR.mkdir(parents=True, exist_ok=True)
    return _FIRMWARE_DIR


def list_firmware_builds() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in sorted(firmware_dir().glob("*.bin"), key=lambda p: p.stat().st_mtime, reverse=True):
        stat = path.stat()
        out.append(
            {
                "filename": path.name,
                "size_bytes": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            }
        )
    return out


def save_firmware_upload(filename: str, data: bytes) -> dict[str, Any]:
    safe = Path(filename).name
    if not safe.endswith(".bin"):
        raise ValueError("Firmware file must be a .bin")
    dest = firmware_dir() / safe
    dest.write_bytes(data)
    return {"filename": safe, "size_bytes": len(data), "path": str(dest)}


def _firmware_url(filename: str) -> str:
    host = os.environ.get("OTA_FIRMWARE_HOST", "").strip()
    port = os.environ.get("NINO_SERVER_PORT", "8000").strip() or "8000"
    if not host:
        from network_util import server_lan_host

        host = server_lan_host()
    return f"http://{host}:{port}/firmware/{Path(filename).name}"


def _load_pending() -> list[dict[str, Any]]:
    if not _PENDING_PATH.is_file():
        return []
    try:
        raw = json.loads(_PENDING_PATH.read_text(encoding="utf-8"))
        return raw if isinstance(raw, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _save_pending(rows: list[dict[str, Any]]) -> None:
    _PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
    _PENDING_PATH.write_text(json.dumps(rows, indent=2), encoding="utf-8")


def request_firmware_deploy(
    *,
    device_id: str,
    filename: str,
    base_url: str,
    requested_by: str = "ops",
    require_approval: bool | None = None,
) -> dict[str, Any]:
    """Queue or immediately trigger OTA to a bot."""
    safe = Path(filename).name
    path = firmware_dir() / safe
    if not path.is_file():
        raise FileNotFoundError(f"Firmware not found: {safe}")

    needs_approval = require_approval
    if needs_approval is None:
        needs_approval = os.environ.get("OTA_REQUIRE_APPROVAL", "1").strip().lower() not in {
            "0",
            "false",
            "no",
        }

    url = _firmware_url(safe)
    record = {
        "approval_id": uuid.uuid4().hex[:12],
        "device_id": device_id,
        "filename": safe,
        "firmware_url": url,
        "base_url": base_url.rstrip("/"),
        "status": "pending" if needs_approval else "approved",
        "requested_by": requested_by,
        "requested_at": _utc_now(),
    }

    if needs_approval:
        rows = _load_pending()
        rows.append(record)
        _save_pending(rows)
        return record

    return deploy_firmware(record)


def list_pending_deployments() -> list[dict[str, Any]]:
    return _load_pending()


def approve_deployment(approval_id: str) -> dict[str, Any]:
    rows = _load_pending()
    target = next((r for r in rows if r.get("approval_id") == approval_id), None)
    if target is None:
        raise KeyError(f"Unknown approval_id: {approval_id}")
    if target.get("status") != "pending":
        return target
    result = deploy_firmware(target)
    rows = [r for r in rows if r.get("approval_id") != approval_id]
    _save_pending(rows)
    return result


def deploy_firmware(record: dict[str, Any]) -> dict[str, Any]:
    base = str(record.get("base_url") or "").rstrip("/")
    url = str(record.get("firmware_url") or "")
    if not base or not url:
        raise ValueError("Missing base_url or firmware_url")

    ota_endpoint = f"{base}/ota/update"
    try:
        resp = requests.post(
            ota_endpoint,
            json={"url": url},
            timeout=15,
        )
        ok = 200 <= resp.status_code < 300
        detail = resp.text[:500]
    except Exception as exc:
        ok = False
        detail = str(exc)

    out = {
        **record,
        "status": "deployed" if ok else "failed",
        "deployed_at": _utc_now(),
        "http_status": getattr(resp, "status_code", 0) if ok or "resp" in locals() else 0,
        "detail": detail,
    }
    logger.info(
        "OTA deploy device=%s file=%s ok=%s endpoint=%s",
        record.get("device_id"),
        record.get("filename"),
        ok,
        ota_endpoint,
    )
    return out


def bot_firmware_status(base_url: str) -> dict[str, Any]:
    base = base_url.rstrip("/")
    try:
        resp = requests.get(f"{base}/status", timeout=5)
        if resp.status_code != 200:
            return {"reachable": False, "http_status": resp.status_code}
        data = resp.json()
        ota = data.get("ota") if isinstance(data.get("ota"), dict) else {}
        return {
            "reachable": True,
            "firmware": data.get("firmware"),
            "device_id": data.get("device_id"),
            "ota_capable": bool(ota.get("capable")),
            "ota_in_progress": bool(ota.get("in_progress")),
        }
    except Exception as exc:
        return {"reachable": False, "error": str(exc)}
