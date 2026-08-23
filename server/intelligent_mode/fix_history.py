"""Learn recovery-action success rates from incident history and reorder chains."""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from intelligent_mode.incidents import Incident, IncidentStore

logger = logging.getLogger(__name__)

_INCIDENTS_PATH = Path(__file__).resolve().parent.parent / "data" / "intelligent_incidents.json"
_stats_cache: dict[str, dict[str, tuple[int, int]]] = {}
_cache_lock = threading.Lock()
_cache_mtime: tuple[float, bool] | None = None


@dataclass(frozen=True)
class FixActionStats:
    action: str
    attempts: int
    successes: int

    @property
    def success_rate(self) -> float:
        if self.attempts <= 0:
            return 0.0
        return self.successes / self.attempts


def _load_incidents(path: Path | None = None) -> list[Incident]:
    store = IncidentStore(path=path or _INCIDENTS_PATH)
    return store.list_incidents(limit=500)


def _fix_counts_as_success(
    inc: Incident,
    fix: Any,
    *,
    verified_only: bool,
) -> bool:
    if not fix.success:
        return False
    if not verified_only:
        return True
    if str(inc.status or "").lower() != "resolved":
        return False
    report = inc.verification_report if isinstance(inc.verification_report, dict) else {}
    return report.get("passed") is True


def compute_fix_stats(
    subsystem: str,
    *,
    incidents: list[Incident] | None = None,
    path: Path | None = None,
    verified_only: bool = False,
) -> dict[str, FixActionStats]:
    """Return per-action attempt/success counts for a subsystem."""
    rows = incidents if incidents is not None else _load_incidents(path)
    subsystem = subsystem.strip().lower()
    counts: dict[str, list[int]] = {}

    for inc in rows:
        if str(inc.subsystem or "").lower() != subsystem:
            continue
        for fix in inc.fixes:
            action = str(fix.action or "").strip()
            if not action or action == "none":
                continue
            bucket = counts.setdefault(action, [0, 0])
            bucket[0] += 1
            if _fix_counts_as_success(inc, fix, verified_only=verified_only):
                bucket[1] += 1

    return {
        action: FixActionStats(action=action, attempts=total, successes=successes)
        for action, (total, successes) in counts.items()
    }


def _refresh_cache_if_needed(path: Path | None = None, *, verified_only: bool = False) -> None:
    global _cache_mtime
    target = path or _INCIDENTS_PATH
    try:
        mtime = target.stat().st_mtime if target.is_file() else 0.0
    except OSError:
        mtime = 0.0

    cache_key = (mtime, verified_only)
    with _cache_lock:
        if _cache_mtime is not None and _cache_mtime == cache_key:
            return
        _stats_cache.clear()
        for inc in _load_incidents(target):
            sub = str(inc.subsystem or "").lower()
            for fix in inc.fixes:
                action = str(fix.action or "").strip()
                if not action or action == "none":
                    continue
                sub_map = _stats_cache.setdefault(sub, {})
                attempts, successes = sub_map.get(action, (0, 0))
                sub_map[action] = (
                    attempts + 1,
                    successes
                    + (1 if _fix_counts_as_success(inc, fix, verified_only=verified_only) else 0),
                )
        _cache_mtime = cache_key


def order_chain_by_success_rate(
    chain: tuple[str, ...],
    subsystem: str,
    *,
    exclude: set[str] | None = None,
    min_samples: int = 2,
    path: Path | None = None,
    verified_only: bool = False,
) -> tuple[str, ...]:
    """Reorder untried recovery actions — highest historical success rate first."""
    if not chain:
        return ()
    exclude = exclude or set()
    untried = [action for action in chain if action not in exclude]
    if len(untried) <= 1:
        return tuple(untried)

    _refresh_cache_if_needed(path, verified_only=verified_only)
    with _cache_lock:
        stats_map = dict(_stats_cache.get(subsystem.lower(), {}))

    def sort_key(action: str) -> tuple[float, int, int]:
        attempts, successes = stats_map.get(action, (0, 0))
        if attempts >= min_samples:
            rate = successes / attempts
            return (-rate, chain.index(action), -attempts)
        return (0.0, chain.index(action), -attempts)

    return tuple(sorted(untried, key=sort_key))


def similar_incidents_summary(
    incident: Incident,
    *,
    limit: int = 5,
    path: Path | None = None,
) -> list[dict[str, Any]]:
    """Recent incidents on the same subsystem for LLM / ops context."""
    rows = _load_incidents(path)
    subsystem = str(incident.subsystem or "").lower()
    device_id = str(incident.device_id or "")
    matches: list[Incident] = []

    for inc in reversed(rows):
        if str(inc.subsystem or "").lower() != subsystem:
            continue
        if inc.incident_id == incident.incident_id:
            continue
        matches.append(inc)
        if len(matches) >= limit:
            break

    out: list[dict[str, Any]] = []
    for inc in matches:
        fixes = [
            {
                "action": fix.action,
                "success": fix.success,
                "detail": str(fix.detail or "")[:120],
            }
            for fix in (inc.fixes or [])[-3:]
        ]
        out.append(
            {
                "device_id": inc.device_id,
                "error": str(inc.error or "")[:160],
                "status": inc.status,
                "fixes": fixes,
            }
        )
    return out


def fix_stats_for_prompt(
    subsystem: str,
    *,
    path: Path | None = None,
    verified_only: bool = False,
    pattern: str | None = None,
) -> list[dict[str, Any]]:
    stats = compute_fix_stats(subsystem, path=path, verified_only=verified_only)
    rows = [
        {
            "action": item.action,
            "attempts": item.attempts,
            "successes": item.successes,
            "success_rate": round(item.success_rate, 3),
            "source": "fix_history",
        }
        for item in sorted(stats.values(), key=lambda s: (-s.success_rate, -s.attempts))
    ]
    if pattern:
        try:
            from intelligent_mode.experience_playbook import playbook_stats_for_prompt

            for item in playbook_stats_for_prompt(pattern)[:6]:
                rows.append({**item, "source": "playbook"})
        except Exception:
            pass
    return rows


def invalidate_stats_cache() -> None:
    global _cache_mtime
    with _cache_lock:
        _stats_cache.clear()
        _cache_mtime = None
