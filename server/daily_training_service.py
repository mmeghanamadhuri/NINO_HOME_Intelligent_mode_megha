"""Nightly training — learn from the day's data without deleting anything."""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from memory_service import (
    parse_summary_scheduler_time,
    seconds_until_local_time,
    yesterday_local_date,
)

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parent / "data"
_TRAINING_DIR = _DATA_DIR / "training"
_LAST_RUN_PATH = _DATA_DIR / "daily_training_last.json"

_service: DailyTrainingService | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _training_enabled() -> bool:
    return os.environ.get("DAILY_TRAINING", "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _training_time() -> str:
    return os.environ.get("DAILY_TRAINING_TIME", "02:00").strip() or "02:00"


def run_daily_training(
    *,
    faces: Any,
    memory_service: Any,
    target_day: date | None = None,
) -> dict[str, Any]:
    """Train from yesterday's data: face embeddings, memories, summaries, export."""
    day = target_day or yesterday_local_date()
    result: dict[str, Any] = {
        "ok": True,
        "day": day.isoformat(),
        "started_at": _utc_now(),
        "face": {},
        "memory": {},
        "summary": {},
        "export": {},
        "errors": [],
    }

    # 1. Face recognition — rebuild embeddings from all stored samples.
    try:
        sample_dirs = list(getattr(faces, "faces_dir", Path()).glob("*/*.jpg"))
        if sample_dirs:
            result["face"] = faces.train()
            logger.info(
                "Daily training: face retrain complete — %s people, %s samples",
                result["face"].get("people"),
                result["face"].get("samples"),
            )
        else:
            result["face"] = {"skipped": True, "reason": "no face samples"}
    except ValueError as exc:
        result["face"] = {"skipped": True, "reason": str(exc)}
    except Exception as exc:
        result["ok"] = False
        result["errors"].append(f"face: {exc}")
        logger.warning("Daily training face step failed: %s", exc)

    # 2. Long-term memory — re-extract facts from yesterday's conversations.
    if memory_service is not None and getattr(memory_service, "ready", False):
        try:
            result["memory"] = memory_service.run_daily_extraction_batch(day)
            logger.info(
                "Daily training: memory batch — %s/%s turns processed for %s",
                result["memory"].get("turns_processed"),
                result["memory"].get("total_turns"),
                day.isoformat(),
            )
        except Exception as exc:
            result["ok"] = False
            result["errors"].append(f"memory: {exc}")
            logger.warning("Daily training memory step failed: %s", exc)

        # 3. Daily summaries — LLM digest per user for yesterday.
        try:
            memory_service._run_summary_catchup_safe()
            result["summary"] = {"ok": True, "day": day.isoformat()}
            logger.info("Daily training: summary catch-up done for %s", day.isoformat())
        except Exception as exc:
            result["ok"] = False
            result["errors"].append(f"summary: {exc}")
            logger.warning("Daily training summary step failed: %s", exc)

        # 4. Export JSONL corpus (kept forever for future fine-tuning).
        try:
            result["export"] = memory_service.export_training_day(day, _TRAINING_DIR)
            logger.info(
                "Daily training: exported %s rows to %s",
                result["export"].get("rows"),
                result["export"].get("daily_file"),
            )
        except Exception as exc:
            result["ok"] = False
            result["errors"].append(f"export: {exc}")
            logger.warning("Daily training export step failed: %s", exc)
    else:
        result["memory"] = {"skipped": True, "reason": "memory not ready"}
        result["summary"] = {"skipped": True, "reason": "memory not ready"}
        result["export"] = {"skipped": True, "reason": "memory not ready"}

    result["finished_at"] = _utc_now()
    _save_last_run(result)
    return result


def _save_last_run(record: dict[str, Any]) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _LAST_RUN_PATH.write_text(json.dumps(record, indent=2), encoding="utf-8")


def load_last_run() -> dict[str, Any] | None:
    if not _LAST_RUN_PATH.is_file():
        return None
    try:
        raw = json.loads(_LAST_RUN_PATH.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


class DailyTrainingService:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._faces: Any = None
        self._memory_service: Any = None

    def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "enabled": _training_enabled(),
            "scheduler_time": _training_time(),
            "training_dir": str(_TRAINING_DIR),
            "last_run": load_last_run(),
        }
        if _training_enabled():
            try:
                hour, minute = parse_summary_scheduler_time(_training_time())
                out["next_run_in_seconds"] = round(
                    seconds_until_local_time(hour, minute), 1
                )
            except ValueError as exc:
                out["scheduler_error"] = str(exc)
        return out

    def start(self, *, faces: Any, memory_service: Any) -> None:
        if not _training_enabled():
            logger.info("Daily training disabled (DAILY_TRAINING=0)")
            return
        self._faces = faces
        self._memory_service = memory_service
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._scheduler_loop,
            daemon=True,
            name="daily-training",
        )
        self._thread.start()
        logger.info(
            "Daily training scheduler started (runs at %s local each night)",
            _training_time(),
        )

    def stop(self) -> None:
        self._stop.set()

    def run_now(self, *, target_day: date | None = None) -> dict[str, Any]:
        if self._faces is None:
            raise RuntimeError("Daily training service not started")
        return run_daily_training(
            faces=self._faces,
            memory_service=self._memory_service,
            target_day=target_day,
        )

    def _scheduler_loop(self) -> None:
        while not self._stop.is_set():
            try:
                hour, minute = parse_summary_scheduler_time(_training_time())
                wait_s = seconds_until_local_time(hour, minute)
                logger.info(
                    "Next daily training in %.0f s (at %s local)",
                    wait_s,
                    _training_time(),
                )
                if self._stop.wait(wait_s):
                    break
                logger.info("Running scheduled daily training")
                run_daily_training(
                    faces=self._faces,
                    memory_service=self._memory_service,
                )
            except ValueError as exc:
                logger.warning("Daily training scheduler disabled: %s", exc)
                return
            except Exception as exc:
                logger.warning("Daily training scheduler error: %s", exc)
                if self._stop.wait(60.0):
                    break


def get_daily_training_service() -> DailyTrainingService:
    global _service
    if _service is None:
        _service = DailyTrainingService()
    return _service


def start_daily_training(*, faces: Any, memory_service: Any) -> DailyTrainingService:
    svc = get_daily_training_service()
    svc.start(faces=faces, memory_service=memory_service)
    return svc
