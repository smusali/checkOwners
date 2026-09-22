"""Webhook notification on drift events."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from checkowners.busfactor import qualified_owner_count_fields
from checkowners.models import Config, DriftEntry, DriftResult, Severity, models_payload

logger = logging.getLogger(__name__)

_SEVERITY_ORDER: tuple[Severity, ...] = ("low", "medium", "high", "critical")


def send_notification(
    result: DriftResult,
    config: Config,
    *,
    severity: Severity | None = None,
    analysis_ref: str = "",
    analysis_epoch: str = "",
) -> bool:
    """POST drift result to the configured webhook URL.

    Returns True if the payload was sent, False if skipped (no webhook URL,
    no drift detected without include_unchanged, or severity below
    severity_threshold) or if the POST itself failed.
    """
    if not config.notifications.webhook_url:
        return False
    if not result.drift_detected and not config.notifications.include_unchanged:
        return False
    resolved = severity if severity is not None else compute_severity(result, config)
    if not _meets_threshold(resolved, config.notifications.severity_threshold):
        return False
    payload = _build_payload(
        result,
        resolved,
        config,
        analysis_ref=analysis_ref,
        analysis_epoch=analysis_epoch,
    )
    return _post_webhook(config.notifications.webhook_url, payload)


def compute_severity(result: DriftResult, config: Config | None = None) -> Severity:
    """Map the max confidence delta + qualified-owner signals to a severity level.

    When `config` is provided the critical qualified-owner signal uses
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


def apply_severity_hysteresis(
    raw: Severity,
    max_delta: float,
    config: Config,
    previous: tuple[Severity | None, Severity | None, int],
) -> tuple[Severity, Severity, int]:
    """Hold a severity flip until it persists or the delta clears a margin.

    Returns ``(reported, pending, streak)``. ``hysteresis_runs <= 1`` or a
    missing prior report is first-run and returns ``raw`` unchanged. A delta
    at least ``2 * min_confidence_delta`` accepts immediately.
    """
    runs = config.drift.hysteresis_runs
    reported, pending, streak = previous
    if runs <= 1 or reported is None:
        return raw, raw, 1
    if max_delta >= config.drift.min_confidence_delta * 2:
        return raw, raw, 1
    if raw == reported:
        return raw, raw, 1
    if raw == pending:
        streak += 1
    else:
        streak = 1
        pending = raw
    if streak >= runs:
        return raw, raw, streak
    return reported, pending, streak


def _has_critical_signal(result: DriftResult, critical_threshold: int) -> bool:
    for entries in (result.stale, result.missing, result.changed):
        for entry in entries:
            if (
                entry.qualified_owner_count is not None
                and entry.qualified_owner_count <= critical_threshold
            ):
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
    *,
    analysis_ref: str = "",
    analysis_epoch: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "analysis_epoch": analysis_epoch,
        "analysis_ref": analysis_ref,
        "models": models_payload(),
        "drift_detected": result.drift_detected,
        "severity": severity,
        "max_confidence_delta": result.max_confidence_delta,
        "stale": [_entry_payload(e, config.analysis.top_n_owners) for e in result.stale],
        "missing": [_entry_payload(e, config.analysis.top_n_owners) for e in result.missing],
        "changed": [_entry_payload(e, config.analysis.top_n_owners) for e in result.changed],
    }
    if config.notifications.include_unchanged:
        payload["include_unchanged"] = True
    return payload


def _entry_payload(entry: DriftEntry, cap: int) -> dict[str, Any]:
    body: dict[str, Any] = {
        "path": entry.path,
        "confidence_delta": entry.confidence_delta,
        "reason": entry.reason,
    }
    if entry.qualified_owner_count is not None:
        body.update(qualified_owner_count_fields(entry.qualified_owner_count, cap))
    if entry.decay:
        body["decay"] = entry.decay
    return body


def _post_webhook(url: str, payload: dict[str, Any]) -> bool:
    """Send an HTTP POST with JSON payload to the given URL.

    Returns True on success, False on any network/HTTP failure. A failed
    delivery never raises.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        logger.warning("Webhook URL scheme %s is not http or https", parsed.scheme)
        return False
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(  # noqa: S310  # scheme restricted to http/https above
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30):  # noqa: S310  # scheme restricted to http/https above
            return True
    except (urllib.error.URLError, OSError) as exc:
        logger.warning("Webhook POST to %s failed: %s", url, exc)
        return False
