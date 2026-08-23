"""SMTP email delivery for intelligent mode reports."""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from intelligent_mode.config import IntelligentConfig
from intelligent_mode.incidents import Incident
from intelligent_mode.reporter import build_html_report, email_subject

logger = logging.getLogger(__name__)


def email_configured(config: IntelligentConfig) -> bool:
    return bool(config.email_to and config.smtp_host)


def send_incident_email(
    incident: Incident,
    report: str,
    *,
    config: IntelligentConfig,
) -> tuple[bool, str]:
    html = build_html_report(incident)
    return send_raw_email(
        email_subject(incident),
        report,
        config=config,
        html=html,
    )


def send_raw_email(
    subject: str,
    report: str,
    *,
    config: IntelligentConfig,
    html: str | None = None,
) -> tuple[bool, str]:
    if not config.email_to:
        return False, "INTELLIGENT_EMAIL_TO not set"
    if not config.smtp_host:
        return False, "INTELLIGENT_SMTP_HOST not set"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config.smtp_from
    msg["To"] = config.email_to
    msg.set_content(report)
    if html:
        msg.add_alternative(html, subtype="html")

    try:
        with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=20) as smtp:
            if config.smtp_use_tls:
                smtp.starttls()
            if config.smtp_user:
                smtp.login(config.smtp_user, config.smtp_password)
            smtp.send_message(msg)
        logger.info("Intelligent mode email sent to %s", config.email_to)
        return True, "sent"
    except Exception as exc:
        logger.warning("Intelligent mode email failed: %s", exc)
        return False, str(exc)
