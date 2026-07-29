"""PC system clock — all alarm scheduling uses this local time."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

DayPart = Literal["morning", "afternoon", "evening", "night"]


def system_now() -> datetime:
    """Naive local date/time from the PC system clock (Windows taskbar time)."""
    return datetime.now()


def system_now_iso() -> str:
    return system_now().isoformat(timespec="seconds")


def day_part(now: datetime | None = None) -> DayPart:
    """Local wall-clock period for greetings (PC system clock)."""
    hour = (now or system_now()).hour
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 22:
        return "evening"
    return "night"


def day_part_greeting(now: datetime | None = None) -> str:
    """Spoken greeting phrase for the current local period."""
    part = day_part(now)
    if part == "morning":
        return "Good morning"
    if part == "afternoon":
        return "Good afternoon"
    if part == "evening":
        return "Good evening"
    # Late night: still greet as evening (not "good night", which sounds like farewell).
    return "Good evening"


def system_clock_info() -> dict:
    """Snapshot for logs and /api/status — confirms which clock alarms use."""
    aware = datetime.now().astimezone()
    offset = aware.utcoffset()
    now = system_now()
    return {
        "now": system_now_iso(),
        "source": "pc_system_clock",
        "timezone_name": aware.tzname() or "local",
        "timezone": str(aware.tzinfo),
        "utc_offset_hours": (offset.total_seconds() / 3600.0) if offset else 0.0,
        "day_part": day_part(now),
        "day_part_greeting": day_part_greeting(now),
    }
