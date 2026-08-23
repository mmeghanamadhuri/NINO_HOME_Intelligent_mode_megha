"""Persist smoke test run history."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from intelligent_mode.smoke_tests import SmokeTestRun


class SmokeTestStore:
    def __init__(self, path: Path | None = None) -> None:
        base = Path(__file__).resolve().parent.parent / "data"
        self._path = path or base / "intelligent_smoke_tests.json"
        self._lock = threading.RLock()
        self._last_run: SmokeTestRun | None = None
        self._history: list[dict[str, Any]] = []
        self._reload()

    def _reload(self) -> None:
        if not self._path.is_file():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._history = list(raw.get("history") or [])
                last = raw.get("last_run")
                if isinstance(last, dict) and last.get("run_id"):
                    self._last_run = SmokeTestRun(
                        run_id=str(last["run_id"]),
                        started_at=str(last.get("started_at") or ""),
                        finished_at=str(last.get("finished_at") or ""),
                        passed=int(last.get("passed") or 0),
                        failed=int(last.get("failed") or 0),
                        total=int(last.get("total") or 0),
                    )
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            self._history = []

    def save_run(self, run: SmokeTestRun) -> None:
        with self._lock:
            self._last_run = run
            entry = run.to_dict()
            self._history.append(entry)
            self._history = self._history[-100:]
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "last_run": entry,
                "history": self._history,
            }
            self._path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def last_run(self) -> SmokeTestRun | None:
        with self._lock:
            return self._last_run

    def last_run_dict(self) -> dict[str, Any] | None:
        with self._lock:
            if not self._path.is_file():
                return None
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                last = raw.get("last_run") if isinstance(raw, dict) else None
                return last if isinstance(last, dict) else None
            except (OSError, json.JSONDecodeError):
                return None

    def history(self, *, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            if self._history:
                return list(self._history[-limit:])
            if not self._path.is_file():
                return []
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                items = raw.get("history") if isinstance(raw, dict) else []
                return list(items[-limit:]) if isinstance(items, list) else []
            except (OSError, json.JSONDecodeError):
                return []
