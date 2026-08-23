"""Learn which recovery actions work for recurring error patterns."""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from intelligent_mode.incidents import Incident

logger = logging.getLogger(__name__)

_PLAYBOOK_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "intelligent_experience_playbook.json"
)
_lock = threading.RLock()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def error_pattern(error: str, subsystem: str) -> str:
    """Normalize free-text errors into stable learnable pattern keys."""
    sub = str(subsystem or "unknown").strip().lower() or "unknown"
    text = re.sub(r"\s+", " ", str(error or "").strip().lower())
    text = re.sub(r"\[soak:[^\]]+\]", "[soak:test]", text)
    text = re.sub(r"incident_id=[a-f0-9]+", "", text).strip()

    if "11434" in text and any(
        token in text for token in ("connection refused", "max retries exceeded")
    ):
        return f"{sub}:ollama_cpu_unreachable"
    if "live voice session active" in text or "soak_live_session_skip" in text:
        return f"{sub}:soak_live_session_skip"
    if "wake_reject" in text:
        return f"{sub}:wake_reject"
    if "wav too large" in text or ("play_wav failed" in text and "too large" in text):
        return f"{sub}:wav_too_large"
    if "unexpected reply" in text:
        return f"{sub}:soak_unexpected_reply"
    if any(token in text for token in ("stt empty", "stt_empty", "no speech", "wake_reject")):
        return f"{sub}:stt_empty"
    if "ollama" in text and "unreachable" in text:
        return f"{sub}:ollama_unreachable"

    slug = re.sub(r"[^a-z0-9]+", "_", text).strip("_")[:72]
    return f"{sub}:{slug or 'unknown'}"


@dataclass(frozen=True)
class PatternActionStats:
    action: str
    attempts: int
    verified_passes: int

    @property
    def verified_rate(self) -> float:
        if self.attempts <= 0:
            return 0.0
        return self.verified_passes / self.attempts


class ExperiencePlaybookStore:
    """Persistent map: error_pattern → action → verified success counts."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _PLAYBOOK_PATH
        self._patterns: dict[str, dict[str, list[int]]] = {}
        self._reload()

    def _reload(self) -> None:
        if not self._path.is_file():
            self._patterns = {}
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            patterns = raw.get("patterns") if isinstance(raw, dict) else {}
            if not isinstance(patterns, dict):
                self._patterns = {}
                return
            cleaned: dict[str, dict[str, list[int]]] = {}
            for pattern, actions in patterns.items():
                if not isinstance(actions, dict):
                    continue
                bucket: dict[str, list[int]] = {}
                for action, counts in actions.items():
                    if not isinstance(counts, list) or len(counts) != 2:
                        continue
                    bucket[str(action)] = [int(counts[0]), int(counts[1])]
                cleaned[str(pattern)] = bucket
            self._patterns = cleaned
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            self._patterns = {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": _utc_now(),
            "patterns": self._patterns,
        }
        self._path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def record(self, pattern: str, action: str, *, verified: bool) -> None:
        action = str(action or "").strip()
        pattern = str(pattern or "").strip()
        if not pattern or not action or action == "none":
            return
        with _lock:
            bucket = self._patterns.setdefault(pattern, {}).setdefault(action, [0, 0])
            bucket[0] += 1
            if verified:
                bucket[1] += 1
            self._save()

    def stats_for(self, pattern: str, action: str) -> PatternActionStats:
        with _lock:
            attempts, passes = self._patterns.get(pattern, {}).get(action, (0, 0))
        return PatternActionStats(
            action=action, attempts=attempts, verified_passes=passes
        )

    def stats_for_pattern(self, pattern: str) -> dict[str, PatternActionStats]:
        with _lock:
            rows = dict(self._patterns.get(pattern, {}))
        return {
            action: PatternActionStats(
                action=action, attempts=counts[0], verified_passes=counts[1]
            )
            for action, counts in rows.items()
        }

    def top_actions(
        self,
        pattern: str,
        candidates: tuple[str, ...],
        *,
        min_samples: int = 2,
    ) -> list[tuple[str, float]]:
        stats = self.stats_for_pattern(pattern)
        ranked: list[tuple[str, float]] = []
        for action in candidates:
            row = stats.get(action)
            if row and row.attempts >= min_samples:
                ranked.append((action, row.verified_rate))
        ranked.sort(key=lambda item: (-item[1], candidates.index(item[0])))
        return ranked


_STORE = ExperiencePlaybookStore()


def get_playbook_store() -> ExperiencePlaybookStore:
    return _STORE


def record_incident_outcome(incident: Incident, action: str, *, verified: bool) -> None:
    pattern = error_pattern(str(incident.error or ""), str(incident.subsystem or ""))
    get_playbook_store().record(pattern, action, verified=verified)


def playbook_stats_for_prompt(pattern: str) -> list[dict[str, Any]]:
    stats = get_playbook_store().stats_for_pattern(pattern)
    return [
        {
            "action": item.action,
            "attempts": item.attempts,
            "verified_passes": item.verified_passes,
            "verified_rate": round(item.verified_rate, 3),
        }
        for item in sorted(
            stats.values(), key=lambda row: (-row.verified_rate, -row.attempts)
        )
    ]


def order_actions_by_experience(
    actions: tuple[str, ...],
    incident: Incident,
    *,
    exclude: set[str] | None = None,
    min_samples: int = 2,
    use_playbook: bool = True,
    use_fix_history: bool = True,
    verified_only: bool = True,
) -> tuple[str, ...]:
    """Reorder candidate actions using playbook + verified fix history."""
    if not actions:
        return ()
    exclude = exclude or set()
    untried = [action for action in actions if action not in exclude]
    if len(untried) <= 1:
        return tuple(untried)

    pattern = error_pattern(str(incident.error or ""), str(incident.subsystem or ""))
    scores: dict[str, float] = {}

    if use_playbook:
        for action, rate in get_playbook_store().top_actions(
            pattern, tuple(untried), min_samples=min_samples
        ):
            scores[action] = max(scores.get(action, 0.0), rate)

    if use_fix_history:
        from intelligent_mode.fix_history import compute_fix_stats

        for action, stat in compute_fix_stats(
            str(incident.subsystem or ""),
            verified_only=verified_only,
        ).items():
            if action in untried and stat.attempts >= min_samples:
                scores[action] = max(scores.get(action, 0.0), stat.success_rate)

    def sort_key(action: str) -> tuple[float, int]:
        return (-scores.get(action, 0.0), actions.index(action))

    return tuple(sorted(untried, key=sort_key))
