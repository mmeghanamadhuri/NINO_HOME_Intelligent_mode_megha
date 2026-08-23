"""Parallel background worker — continuously processes code bugs alongside the server."""

from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from intelligent_mode.config import IntelligentConfig, load_config
from intelligent_mode.incidents import Incident

logger = logging.getLogger(__name__)

_WORKER: CodingAgentWorker | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, str(int(default))).strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return max(minimum, default)


@dataclass
class WorkerStats:
    cycles: int = 0
    incidents_scanned: int = 0
    proposals_created: int = 0
    proposals_failed: int = 0
    emails_sent: int = 0
    in_flight: int = 0
    last_cycle_at: str = ""
    last_error: str = ""
    active_incident_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cycles": self.cycles,
            "incidents_scanned": self.incidents_scanned,
            "proposals_created": self.proposals_created,
            "proposals_failed": self.proposals_failed,
            "emails_sent": self.emails_sent,
            "in_flight": self.in_flight,
            "last_cycle_at": self.last_cycle_at,
            "last_error": self.last_error,
            "active_incident_ids": list(self.active_incident_ids),
        }


class CodingAgentWorker:
    """Runs in parallel with NiNO server — scans developer queue and proposes fixes."""

    def __init__(
        self,
        *,
        poll_seconds: int = 30,
        parallel_workers: int = 2,
        config: IntelligentConfig | None = None,
        get_incidents: Any = None,
    ) -> None:
        self.poll_seconds = max(15, poll_seconds)
        self.parallel_workers = max(1, min(parallel_workers, 6))
        self.config = config or load_config()
        self._get_incidents = get_incidents
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pool: ThreadPoolExecutor | None = None
        self._futures: dict[str, Future] = {}
        self._futures_lock = threading.Lock()
        self.stats = WorkerStats()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._pool = ThreadPoolExecutor(
            max_workers=self.parallel_workers,
            thread_name_prefix="nino-coding-agent",
        )
        self._thread = threading.Thread(target=self._loop, daemon=True, name="nino-coding-agent-loop")
        self._thread.start()
        logger.info(
            "Coding agent worker started (poll=%ss, parallel=%d)",
            self.poll_seconds,
            self.parallel_workers,
        )

    def stop(self) -> None:
        self._stop.set()
        with self._futures_lock:
            for fut in self._futures.values():
                fut.cancel()
            self._futures.clear()
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10.0)
        self._thread = None
        logger.info("Coding agent worker stopped")

    def status(self) -> dict[str, Any]:
        from intelligent_mode.coding_agent import list_proposals, select_coding_model

        model, reason = select_coding_model()
        pending = len(list_proposals(status="pending"))
        applied = len(list_proposals(status="applied"))
        return {
            "running": self.running,
            "poll_seconds": self.poll_seconds,
            "parallel_workers": self.parallel_workers,
            "model": model,
            "model_reason": reason,
            "pending_proposals": pending,
            "applied_proposals": applied,
            "stats": self.stats.to_dict(),
        }

    def _collect_code_bug_incidents(self) -> list[Incident]:
        if self._get_incidents is None:
            return []
        try:
            incidents = self._get_incidents()
        except Exception as exc:
            self.stats.last_error = str(exc)
            return []
        out: list[Incident] = []
        for inc in incidents:
            if str(inc.status or "") not in {"open", "fixing", "escalated"}:
                continue
            try:
                from intelligent_mode.code_bug_analyzer import is_code_bug_incident

                if is_code_bug_incident(inc):
                    out.append(inc)
            except Exception:
                continue
        return out

    def _already_handled(self, incident_id: str) -> bool:
        from intelligent_mode.coding_agent import list_proposals

        for proposal in list_proposals(limit=500):
            if proposal.incident_id != incident_id:
                continue
            if proposal.status in {"pending", "approved", "applied"}:
                return True
        with self._futures_lock:
            if incident_id in self._futures and not self._futures[incident_id].done():
                return True
        return False

    def _process_incident(self, incident: Incident) -> dict[str, Any]:
        from intelligent_mode.coding_agent import process_code_bug_incident_smart

        try:
            proposal = process_code_bug_incident_smart(incident, config=self.config)
            if proposal is None:
                return {"ok": False, "incident_id": incident.incident_id, "error": "no proposal"}
            return {
                "ok": True,
                "incident_id": incident.incident_id,
                "proposal_id": proposal.proposal_id,
                "confidence": getattr(proposal, "confidence", ""),
                "changes": len(proposal.changes),
                "email_sent": proposal.email_sent,
            }
        except Exception as exc:
            logger.exception("Coding agent worker failed for %s", incident.incident_id)
            return {"ok": False, "incident_id": incident.incident_id, "error": str(exc)}

    def _drain_futures(self) -> None:
        done_ids: list[str] = []
        with self._futures_lock:
            for inc_id, fut in list(self._futures.items()):
                if not fut.done():
                    continue
                done_ids.append(inc_id)
                try:
                    result = fut.result()
                    if result.get("ok"):
                        self.stats.proposals_created += 1
                        if result.get("email_sent"):
                            self.stats.emails_sent += 1
                    else:
                        self.stats.proposals_failed += 1
                except Exception as exc:
                    self.stats.proposals_failed += 1
                    self.stats.last_error = str(exc)
            for inc_id in done_ids:
                self._futures.pop(inc_id, None)
            self.stats.in_flight = len(self._futures)
            self.stats.active_incident_ids = list(self._futures.keys())

    def run_once(self) -> dict[str, Any]:
        """Scan queue and dispatch parallel fix jobs."""
        self._drain_futures()
        incidents = self._collect_code_bug_incidents()
        self.stats.incidents_scanned = len(incidents)
        dispatched = 0

        if self._pool is None:
            self._pool = ThreadPoolExecutor(
                max_workers=self.parallel_workers,
                thread_name_prefix="nino-coding-agent",
            )

        for inc in incidents:
            if self._already_handled(inc.incident_id):
                continue
            with self._futures_lock:
                if len(self._futures) >= self.parallel_workers:
                    break
                fut = self._pool.submit(self._process_incident, inc)
                self._futures[inc.incident_id] = fut
                dispatched += 1

        self.stats.in_flight = len(self._futures)
        self.stats.active_incident_ids = list(self._futures.keys())
        self.stats.cycles += 1
        self.stats.last_cycle_at = _utc_now()
        return {"scanned": len(incidents), "dispatched": dispatched, "in_flight": len(self._futures)}

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                summary = self.run_once()
                if summary.get("dispatched") or summary.get("in_flight"):
                    logger.info("Coding agent cycle: %s", summary)
            except Exception as exc:
                self.stats.last_error = str(exc)
                logger.exception("Coding agent worker cycle failed")
            self._stop.wait(self.poll_seconds)


def get_coding_agent_worker() -> CodingAgentWorker | None:
    return _WORKER


def configure_coding_agent_worker(worker: CodingAgentWorker | None) -> None:
    global _WORKER
    _WORKER = worker


def start_coding_agent_worker(*, get_incidents: Any = None) -> CodingAgentWorker | None:
    if not _env_bool("CODING_AGENT_ENABLED", False):
        return None
    global _WORKER
    if _WORKER is not None and _WORKER.running:
        return _WORKER
    cfg = load_config()
    worker = CodingAgentWorker(
        poll_seconds=_env_int("CODING_AGENT_POLL_SECONDS", 30, minimum=15),
        parallel_workers=_env_int("CODING_AGENT_PARALLEL", 2, minimum=1),
        config=cfg,
        get_incidents=get_incidents,
    )
    configure_coding_agent_worker(worker)
    worker.start()
    return worker


def stop_coding_agent_worker() -> None:
    global _WORKER
    if _WORKER is not None:
        _WORKER.stop()
        _WORKER = None
