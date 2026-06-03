"""PC system clock — all alarm scheduling uses this local time."""

from __future__ import annotations

from datetime import datetime


def system_now() -> datetime:
    """Naive local date/time from the PC system clock (Windows taskbar time)."""
    return datetime.now()


def system_now_iso() -> str:
    return system_now().isoformat(timespec="seconds")


def system_clock_info() -> dict:
    """Snapshot for logs and /api/status — confirms which clock alarms use."""
    aware = datetime.now().astimezone()
    offset = aware.utcoffset()
    return {
        "now": system_now_iso(),
        "source": "pc_system_clock",
        "timezone_name": aware.tzname() or "local",
        "timezone": str(aware.tzinfo),
        "utc_offset_hours": (offset.total_seconds() / 3600.0) if offset else 0.0,
    }
