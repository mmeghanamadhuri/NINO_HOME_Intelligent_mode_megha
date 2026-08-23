"""Environment-driven configuration for intelligent mode."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, str(int(default))).strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return max(minimum, default)


@dataclass(frozen=True)
class IntelligentConfig:
    enabled: bool = False
    poll_seconds: int = 45
    grace_seconds: int = 90
    verify_delay_seconds: int = 12
    max_fix_attempts_per_hour: int = 3
    email_to: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_use_tls: bool = True
    llm_reports: bool = True
    email_on_resolve: bool = True
    email_on_escalate: bool = True
    smoke_tests_enabled: bool = True
    e2e_tests_enabled: bool = True
    self_debug_enabled: bool = True
    skip_fix_during_voice: bool = True
    skip_e2e_during_voice: bool = True
    llm_debug_analysis: bool = True
    email_mode: str = "digest"
    email_digest_seconds: int = 900
    camera_grace_seconds: int = 120
    llm_grace_seconds: int = 120
    fix_cooldown_seconds: int = 60
    max_auto_fix_tier: int = 1
    autonomous_recovery_enabled: bool = False
    recovery_chain_max_steps: int = 2
    autonomous_max_fix_tier: int = 2
    retry_escalated: bool = True
    email_code_bugs: bool = True
    auto_ota_on_code_bug: bool = False
    fix_history_enabled: bool = True
    min_fix_history_samples: int = 2
    llm_fix_selection: bool = False
    llm_fix_min_confidence: str = "medium"
    baseline_anomaly_enabled: bool = True
    baseline_sigma_threshold: float = 3.0
    baseline_min_samples: int = 20
    baseline_grace_seconds: int = 120
    verification_live_probes: bool = True
    experience_playbook_enabled: bool = True
    learn_from_verification: bool = True
    prune_resolved_on_start: bool = False
    incidents_keep_resolved: int = 50
    incidents_store_max: int = 10000
    stt_empty_incident_threshold: int = 3
    stt_empty_window_seconds: int = 600
    latency_failure_max_age_seconds: int = 900
    coding_agent_enabled: bool = False
    coding_agent_email: bool = True


def load_config() -> IntelligentConfig:
    email_to = os.environ.get("INTELLIGENT_EMAIL_TO", "").strip()
    smtp_from = os.environ.get("INTELLIGENT_SMTP_FROM", "").strip()
    smtp_user = os.environ.get("INTELLIGENT_SMTP_USER", "").strip()
    enabled = _env_bool("INTELLIGENT_MODE", False)
    email_mode = os.environ.get("INTELLIGENT_EMAIL_MODE", "digest").strip().lower()
    if email_mode not in {"immediate", "digest"}:
        email_mode = "digest"
    try:
        baseline_sigma = float(os.environ.get("INTELLIGENT_BASELINE_SIGMA", "3.0") or "3.0")
    except (TypeError, ValueError):
        baseline_sigma = 3.0
    llm_fix_conf = os.environ.get("INTELLIGENT_LLM_FIX_MIN_CONFIDENCE", "medium").strip().lower()
    if llm_fix_conf not in {"low", "medium", "high"}:
        llm_fix_conf = "medium"
    return IntelligentConfig(
        enabled=enabled,
        poll_seconds=_env_int("INTELLIGENT_POLL_SECONDS", 45, minimum=15),
        grace_seconds=_env_int("INTELLIGENT_GRACE_SECONDS", 90, minimum=30),
        verify_delay_seconds=_env_int("INTELLIGENT_VERIFY_DELAY_SECONDS", 12, minimum=5),
        max_fix_attempts_per_hour=_env_int("INTELLIGENT_MAX_FIX_ATTEMPTS", 3, minimum=1),
        email_to=email_to,
        smtp_host=os.environ.get("INTELLIGENT_SMTP_HOST", "").strip(),
        smtp_port=_env_int("INTELLIGENT_SMTP_PORT", 587, minimum=1),
        smtp_user=smtp_user,
        smtp_password=os.environ.get("INTELLIGENT_SMTP_PASSWORD", "").strip(),
        smtp_from=smtp_from or smtp_user or "nino-server@localhost",
        smtp_use_tls=_env_bool("INTELLIGENT_SMTP_TLS", True),
        llm_reports=_env_bool("INTELLIGENT_LLM_REPORTS", True),
        email_on_resolve=_env_bool("INTELLIGENT_EMAIL_ON_RESOLVE", True),
        email_on_escalate=_env_bool("INTELLIGENT_EMAIL_ON_ESCALATE", True),
        smoke_tests_enabled=_env_bool("INTELLIGENT_SMOKE_TESTS", enabled),
        e2e_tests_enabled=_env_bool("INTELLIGENT_E2E_TESTS", enabled),
        self_debug_enabled=_env_bool("INTELLIGENT_SELF_DEBUG", enabled),
        skip_fix_during_voice=_env_bool("INTELLIGENT_SKIP_FIX_DURING_VOICE", True),
        skip_e2e_during_voice=_env_bool("INTELLIGENT_SKIP_E2E_DURING_VOICE", True),
        llm_debug_analysis=_env_bool("INTELLIGENT_LLM_DEBUG", True),
        email_mode=email_mode,
        email_digest_seconds=_env_int("INTELLIGENT_EMAIL_DIGEST_SECONDS", 900, minimum=60),
        camera_grace_seconds=_env_int("INTELLIGENT_CAMERA_GRACE_SECONDS", 120, minimum=30),
        llm_grace_seconds=_env_int("INTELLIGENT_LLM_GRACE_SECONDS", 120, minimum=30),
        fix_cooldown_seconds=_env_int("INTELLIGENT_FIX_COOLDOWN_SECONDS", 60, minimum=15),
        max_auto_fix_tier=_env_int("INTELLIGENT_MAX_AUTO_FIX_TIER", 1, minimum=0),
        autonomous_recovery_enabled=_env_bool(
            "INTELLIGENT_AUTONOMOUS_RECOVERY",
            _env_bool("INTELLIGENT_MODE", False),
        ),
        recovery_chain_max_steps=_env_int(
            "INTELLIGENT_RECOVERY_CHAIN_STEPS", 2, minimum=1
        ),
        autonomous_max_fix_tier=_env_int(
            "INTELLIGENT_AUTONOMOUS_MAX_TIER", 2, minimum=0
        ),
        retry_escalated=_env_bool("INTELLIGENT_RETRY_ESCALATED", True),
        email_code_bugs=_env_bool("INTELLIGENT_EMAIL_CODE_BUGS", enabled),
        auto_ota_on_code_bug=_env_bool("INTELLIGENT_AUTO_OTA", False),
        fix_history_enabled=_env_bool("INTELLIGENT_FIX_HISTORY", True),
        min_fix_history_samples=_env_int("INTELLIGENT_FIX_HISTORY_MIN_SAMPLES", 2, minimum=1),
        llm_fix_selection=_env_bool("INTELLIGENT_LLM_FIX_SELECTION", False),
        llm_fix_min_confidence=llm_fix_conf,
        baseline_anomaly_enabled=_env_bool("INTELLIGENT_BASELINE_ANOMALY", True),
        baseline_sigma_threshold=max(1.0, baseline_sigma),
        baseline_min_samples=_env_int("INTELLIGENT_BASELINE_MIN_SAMPLES", 20, minimum=5),
        baseline_grace_seconds=_env_int("INTELLIGENT_BASELINE_GRACE_SECONDS", 120, minimum=30),
        verification_live_probes=_env_bool("INTELLIGENT_VERIFICATION_LIVE_PROBES", True),
        experience_playbook_enabled=_env_bool("INTELLIGENT_EXPERIENCE_PLAYBOOK", True),
        learn_from_verification=_env_bool("INTELLIGENT_LEARN_FROM_VERIFICATION", True),
        prune_resolved_on_start=_env_bool("INTELLIGENT_PRUNE_ON_START", False),
        incidents_keep_resolved=_env_int("INTELLIGENT_INCIDENTS_KEEP_RESOLVED", 50, minimum=10),
        incidents_store_max=_env_int("INTELLIGENT_INCIDENTS_STORE_MAX", 10000, minimum=500),
        stt_empty_incident_threshold=_env_int("INTELLIGENT_STT_EMPTY_THRESHOLD", 3, minimum=2),
        stt_empty_window_seconds=_env_int("INTELLIGENT_STT_EMPTY_WINDOW_SECONDS", 600, minimum=60),
        latency_failure_max_age_seconds=_env_int(
            "INTELLIGENT_LATENCY_FAILURE_MAX_AGE_SECONDS", 900, minimum=60
        ),
        coding_agent_enabled=_env_bool("CODING_AGENT_ENABLED", False),
        coding_agent_email=_env_bool("CODING_AGENT_EMAIL", True),
    )
