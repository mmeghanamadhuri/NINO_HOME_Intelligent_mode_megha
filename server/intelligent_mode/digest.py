"""Email digest queue — reduces alert fatigue by batching non-critical mail."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from intelligent_mode.config import IntelligentConfig
from intelligent_mode.emailer import email_configured, send_raw_email
from intelligent_mode.incidents import Incident
from intelligent_mode.reporter import build_digest_html, build_digest_report

logger = logging.getLogger(__name__)


class EmailDigestQueue:
    def __init__(self, config: IntelligentConfig) -> None:
        self._config = config
        self._pending: list[Incident] = []
        self._last_flush_at = time.time()

    def reset_timer(self) -> None:
        self._last_flush_at = time.time()

    def pending_count(self) -> int:
        return len(self._pending)

    def enqueue(self, incident: Incident) -> bool:
        """Return True if queued for digest; False if caller should send immediately."""
        if not email_configured(self._config):
            return False
        if self._config.email_mode == "immediate":
            return False
        if incident.severity == "critical" and incident.status in {
            "open",
            "escalated",
        }:
            return False
        self._pending.append(incident)
        return True

    def flush_due(self) -> list[Incident]:
        if not self._pending or not email_configured(self._config):
            return []
        if self._config.email_mode != "digest":
            return []
        elapsed = time.time() - self._last_flush_at
        if elapsed < self._config.email_digest_seconds:
            return []
        return self.flush_now()

    def flush_now(self) -> list[Incident]:
        if not self._pending or not email_configured(self._config):
            return []
        batch = list(self._pending)
        self._pending.clear()
        self._last_flush_at = time.time()
        report = build_digest_report(batch, use_llm=self._config.llm_reports)
        html = build_digest_html(batch)
        subject = (
            f"[NiNO] Summary — {len(batch)} alert(s) — "
            f"{datetime.now(timezone.utc).strftime('%d %b %Y %H:%M UTC')}"
        )
        ok, detail = send_raw_email(subject, report, config=self._config, html=html)
        if ok:
            logger.info("Intelligent mode digest sent (%d incidents)", len(batch))
            for inc in batch:
                inc.email_sent = True
            return batch
        logger.warning("Intelligent mode digest failed: %s", detail)
        self._pending[:0] = batch
        return []
