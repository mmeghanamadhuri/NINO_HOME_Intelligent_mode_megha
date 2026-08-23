"""Incident model and durable JSON store."""

from __future__ import annotations

import json
import re
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_signature(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "").strip().lower())
    return cleaned[:160] or "unknown"


@dataclass
class FixAttempt:
    action: str
    success: bool
    detail: str
    at: str = field(default_factory=_utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Incident:
    device_id: str
    display_name: str
    subsystem: str
    severity: str
    tier: int
    error: str
    signature: str
    status: str = "open"
    incident_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    detected_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)
    resolved_at: str | None = None
    fix_attempts: int = 0
    fixes: list[FixAttempt] = field(default_factory=list)
    report: str = ""
    email_sent: bool = False
    debug_report: dict[str, Any] | None = None
    before_snapshot: dict[str, Any] = field(default_factory=dict)
    after_snapshot: dict[str, Any] | None = None
    verification_report: dict[str, Any] | None = None

    @staticmethod
    def make_signature(device_id: str, subsystem: str, error: str) -> str:
        return f"{device_id}:{subsystem}:{_normalize_signature(error)}"

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["fixes"] = [f if isinstance(f, dict) else f.to_dict() for f in self.fixes]
        return payload

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Incident:
        fixes_raw = raw.get("fixes") or []
        fixes = [
            FixAttempt(**item) if isinstance(item, dict) else item for item in fixes_raw
        ]
        return cls(
            incident_id=str(raw.get("incident_id") or uuid.uuid4().hex[:12]),
            device_id=str(raw.get("device_id") or "server"),
            display_name=str(raw.get("display_name") or ""),
            subsystem=str(raw.get("subsystem") or "unknown"),
            severity=str(raw.get("severity") or "warning"),
            tier=int(raw.get("tier") or 2),
            error=str(raw.get("error") or ""),
            signature=str(raw.get("signature") or ""),
            status=str(raw.get("status") or "open"),
            detected_at=str(raw.get("detected_at") or _utc_now()),
            updated_at=str(raw.get("updated_at") or _utc_now()),
            resolved_at=raw.get("resolved_at"),
            fix_attempts=int(raw.get("fix_attempts") or 0),
            fixes=fixes,
            report=str(raw.get("report") or ""),
            email_sent=bool(raw.get("email_sent")),
            debug_report=dict(raw["debug_report"]) if raw.get("debug_report") else None,
            before_snapshot=dict(raw.get("before_snapshot") or {}),
            after_snapshot=dict(raw["after_snapshot"])
            if raw.get("after_snapshot")
            else None,
            verification_report=dict(raw["verification_report"])
            if raw.get("verification_report")
            else None,
        )


class IncidentStore:
    def __init__(self, path: Path | None = None, *, store_max: int = 10000) -> None:
        base = Path(__file__).resolve().parent.parent / "data"
        self._path = path or base / "intelligent_incidents.json"
        self._store_max = max(500, int(store_max))
        self._lock = threading.RLock()
        self._incidents: list[Incident] = []
        self._reload()

    def _reload(self) -> None:
        if not self._path.is_file():
            self._incidents = []
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            items = raw.get("incidents") if isinstance(raw, dict) else raw
            if not isinstance(items, list):
                self._incidents = []
                return
            self._incidents = [Incident.from_dict(item) for item in items if isinstance(item, dict)]
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            self._incidents = []

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        cap = max(500, int(getattr(self, "_store_max", 10000) or 10000))
        payload = {
            "updated_at": _utc_now(),
            "incidents": [inc.to_dict() for inc in self._incidents[-cap:]],
        }
        self._path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def set_store_max(self, maximum: int) -> None:
        self._store_max = max(500, int(maximum))

    def reload(self) -> None:
        with self._lock:
            self._reload()

    def list_incidents(self, *, limit: int = 50) -> list[Incident]:
        with self._lock:
            return list(self._incidents[-limit:])

    def open_incident(self, candidate: Incident) -> tuple[Incident, bool]:
        with self._lock:
            for inc in reversed(self._incidents):
                if inc.signature == candidate.signature and inc.status in {
                    "open",
                    "fixing",
                    "escalated",
                }:
                    inc.updated_at = _utc_now()
                    if candidate.error and candidate.error not in inc.error:
                        inc.error = candidate.error
                    self._save()
                    return inc, False
            self._incidents.append(candidate)
            self._save()
            return candidate, True

    def update(self, incident: Incident) -> None:
        with self._lock:
            for idx, inc in enumerate(self._incidents):
                if inc.incident_id == incident.incident_id:
                    incident.updated_at = _utc_now()
                    self._incidents[idx] = incident
                    self._save()
                    return
            self._incidents.append(incident)
            self._save()

    def recent_fix_count(self, signature: str, *, window_seconds: int = 3600) -> int:
        cutoff = datetime.now(timezone.utc).timestamp() - window_seconds
        count = 0
        with self._lock:
            for inc in self._incidents:
                if inc.signature != signature:
                    continue
                for fix in inc.fixes:
                    try:
                        ts = datetime.fromisoformat(fix.at.replace("Z", "+00:00")).timestamp()
                    except ValueError:
                        continue
                    if ts >= cutoff:
                        count += 1
        return count

    def prune_resolved(
        self,
        *,
        keep_recent: int = 50,
        archive: bool = True,
    ) -> dict[str, int]:
        """Remove resolved benign/noise incidents; keep recent real history."""
        from intelligent_mode.incident_filters import is_benign_resolved_incident

        removed: list[Incident] = []
        kept: list[Incident] = []
        with self._lock:
            active: list[Incident] = []
            resolved: list[Incident] = []
            for inc in self._incidents:
                if inc.status in {"open", "fixing", "escalated"}:
                    active.append(inc)
                elif inc.status == "resolved":
                    resolved.append(inc)
                else:
                    kept.append(inc)

            real_resolved: list[Incident] = []
            for inc in resolved:
                if is_benign_resolved_incident(inc):
                    removed.append(inc)
                else:
                    real_resolved.append(inc)

            if len(real_resolved) > keep_recent:
                removed.extend(real_resolved[:-keep_recent])
                real_resolved = real_resolved[-keep_recent:]

            if archive and removed:
                archive_path = self._path.with_name("intelligent_incidents_archive.json")
                existing: list[dict] = []
                if archive_path.is_file():
                    try:
                        raw = json.loads(archive_path.read_text(encoding="utf-8"))
                        items = raw.get("incidents") if isinstance(raw, dict) else raw
                        if isinstance(items, list):
                            existing = [i for i in items if isinstance(i, dict)]
                    except (OSError, json.JSONDecodeError):
                        existing = []
                existing.extend(inc.to_dict() for inc in removed)
                archive_path.write_text(
                    json.dumps(
                        {"updated_at": _utc_now(), "incidents": existing[-2000:]},
                        indent=2,
                    ),
                    encoding="utf-8",
                )

            self._incidents = active + real_resolved + kept
            remaining = len(self._incidents)
            self._save()

        return {
            "removed": len(removed),
            "remaining": remaining,
            "archived": len(removed) if archive else 0,
        }

    def restore_from_archive(self, *, clear_archive: bool = False) -> dict[str, int]:
        """Merge archived incidents back into the active store (never deletes archive by default)."""
        archive_path = self._path.with_name("intelligent_incidents_archive.json")
        if not archive_path.is_file():
            return {"restored": 0, "total": len(self._incidents)}
        try:
            raw = json.loads(archive_path.read_text(encoding="utf-8"))
            items = raw.get("incidents") if isinstance(raw, dict) else raw
            if not isinstance(items, list):
                return {"restored": 0, "total": len(self._incidents)}
        except (OSError, json.JSONDecodeError):
            return {"restored": 0, "total": len(self._incidents)}

        restored = 0
        with self._lock:
            by_id = {inc.incident_id: inc for inc in self._incidents}
            for item in items:
                if not isinstance(item, dict):
                    continue
                iid = str(item.get("incident_id") or "")
                if not iid or iid in by_id:
                    continue
                by_id[iid] = Incident.from_dict(item)
                restored += 1
            self._incidents = list(by_id.values())
            self._incidents.sort(key=lambda i: i.detected_at or "")
            self._save()
            if clear_archive:
                archive_path.unlink(missing_ok=True)
        return {"restored": restored, "total": len(self._incidents)}
