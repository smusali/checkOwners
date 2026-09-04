"""Webhook notification on drift events."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from checkowners.models import Config, DriftEntry, DriftResult, Severity

logger = logging.getLogger(__name__)

_SEVERITY_ORDER: tuple[Severity, ...] = ("low", "medium", "high", "critical")


def send_notification(result: DriftResult, config: Config) -> bool:
    """POST drift result to the configured webhook URL.

    Returns True if the payload was sent, False if skipped (no webhook URL,
    no drift detected without include_unchanged, or severity below
    severity_threshold) or if the POST itself failed.
    """
    if not config.notifications.webhook_url:
        return False
    if not result.drift_detected and not config.notifications.include_unchanged:
        return False
    severity = compute_severity(result, config)
    if not _meets_threshold(severity, config.notifications.severity_threshold):
        return False
    payload = _build_payload(result, severity, config)
    return _post_webhook(config.notifications.webhook_url, payload)


def compute_severity(result: DriftResult, config: Config | None = None) -> Severity:
    """Map the max confidence delta + reviewer depth signals to a severity level.

    When `config` is provided the critical reviewer depth signal uses
    `config.bus_factor.critical_threshold`; otherwise it falls back to 1.
    """
    critical_threshold = config.bus_factor.critical_threshold if config is not None else 1
    if _has_critical_signal(result, critical_threshold):
        return "critical"
    delta = result.max_confidence_delta
    if delta >= 0.7:
        return "high"
    if delta >= 0.3:
        return "medium"
    return "low"


def _has_critical_signal(result: DriftResult, critical_threshold: int) -> bool:
    for entries in (result.stale, result.missing, result.changed):
        for entry in entries:
            if entry.bus_factor is not None and entry.bus_factor <= critical_threshold:
                return True
            if entry.decay:
                return True
    return False


def _meets_threshold(severity: Severity, threshold: Severity) -> bool:
    return _SEVERITY_ORDER.index(severity) >= _SEVERITY_ORDER.index(threshold)


def _build_payload(
    result: DriftResult,
    severity: Severity,
    config: Config,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "drift_detected": result.drift_detected,
        "severity": severity,
        "max_confidence_delta": result.max_confidence_delta,
        "stale": [_entry_payload(e) for e in result.stale],
        "missing": [_entry_payload(e) for e in result.missing],
        "changed": [_entry_payload(e) for e in result.changed],
    }
    if config.notifications.include_unchanged:
        payload["include_unchanged"] = True
    return payload


def _entry_payload(entry: DriftEntry) -> dict[str, Any]:
    body: dict[str, Any] = {
        "path": entry.path,
        "confidence_delta": entry.confidence_delta,
        "reason": entry.reason,
    }
    if entry.bus_factor is not None:
        body["bus_factor"] = entry.bus_factor
    if entry.decay:
        body["decay"] = entry.decay
    return body


def _post_webhook(url: str, payload: dict[str, Any]) -> bool:
    """Send an HTTP POST with JSON payload to the given URL.

    Returns True on success, False on any network/HTTP failure. A failed
    delivery never raises.
    """
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30):  # noqa: S310
            pass
    except (urllib.error.URLError, OSError) as exc:
        logger.warning("Webhook POST to %s failed: %s", url, exc)
        return False
    return True
