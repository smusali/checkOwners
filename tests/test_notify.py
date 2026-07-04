"""Tests for checkowners.notify module."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from unittest.mock import MagicMock, patch

from checkowners.models import (
    BusFactorConfig,
    Config,
    DriftEntry,
    DriftResult,
    NotificationsConfig,
)
from checkowners.notify import (
    _build_payload,
    compute_severity,
    send_notification,
)


def _drift_with(
    *,
    delta: float = 1.0,
    bus_factor: int | None = None,
    decay: bool = False,
    detected: bool = True,
) -> DriftResult:
    if not detected:
        return DriftResult(stale=(), missing=(), changed=(), drift_detected=False)
    entry = DriftEntry(
        path="/src/main.py",
        confidence_delta=delta,
        reason="test",
        bus_factor=bus_factor,
        decay=decay,
    )
    return DriftResult(stale=(entry,), missing=(), changed=(), drift_detected=True)


def test_send_notification_skips_empty_url() -> None:
    config = Config(notifications=NotificationsConfig(webhook_url=""))
    assert send_notification(_drift_with(), config) is False


def test_send_notification_posts_webhook() -> None:
    config = Config(
        notifications=NotificationsConfig(
            webhook_url="https://hooks.example.com/drift",
            severity_threshold="low",
        ),
    )
    with patch("checkowners.notify.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = MagicMock()
        sent = send_notification(_drift_with(delta=0.9), config)
    assert sent is True
    mock_urlopen.assert_called_once()
    req = mock_urlopen.call_args[0][0]
    assert req.full_url == "https://hooks.example.com/drift"
    assert req.get_header("Content-type") == "application/json"


def test_send_notification_skipped_when_below_threshold() -> None:
    config = Config(
        notifications=NotificationsConfig(
            webhook_url="https://hooks.example.com/drift",
            severity_threshold="critical",
        ),
    )
    with patch("checkowners.notify.urllib.request.urlopen") as mock_urlopen:
        sent = send_notification(_drift_with(delta=0.2), config)
    assert sent is False
    mock_urlopen.assert_not_called()


def test_compute_severity_low_medium_high_critical() -> None:
    assert compute_severity(_drift_with(delta=0.1)) == "low"
    assert compute_severity(_drift_with(delta=0.4)) == "medium"
    assert compute_severity(_drift_with(delta=0.8)) == "high"
    assert compute_severity(_drift_with(delta=0.8, bus_factor=1)) == "critical"
    assert compute_severity(_drift_with(delta=0.1, decay=True)) == "critical"


def test_compute_severity_no_drift_is_low() -> None:
    assert compute_severity(_drift_with(detected=False)) == "low"


def test_compute_severity_uses_configured_critical_threshold() -> None:
    config = Config(bus_factor=BusFactorConfig(critical_threshold=2, warn_threshold=3))
    # bus_factor=2 is critical under the configured threshold...
    assert compute_severity(_drift_with(delta=0.1, bus_factor=2), config) == "critical"
    # ...but not under the default fallback of 1.
    assert compute_severity(_drift_with(delta=0.1, bus_factor=2)) == "low"
    assert compute_severity(_drift_with(delta=0.1, bus_factor=1)) == "critical"


def test_send_notification_skips_when_no_drift() -> None:
    config = Config(
        notifications=NotificationsConfig(
            webhook_url="https://hooks.example.com/drift",
            severity_threshold="low",
        ),
    )
    with patch("checkowners.notify.urllib.request.urlopen") as mock_urlopen:
        sent = send_notification(_drift_with(detected=False), config)
    assert sent is False
    mock_urlopen.assert_not_called()


def test_send_notification_no_drift_sent_with_include_unchanged() -> None:
    config = Config(
        notifications=NotificationsConfig(
            webhook_url="https://hooks.example.com/drift",
            severity_threshold="low",
            include_unchanged=True,
        ),
    )
    with patch("checkowners.notify.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = MagicMock()
        sent = send_notification(_drift_with(detected=False), config)
    assert sent is True
    mock_urlopen.assert_called_once()


def test_send_notification_returns_false_on_network_failure() -> None:
    config = Config(
        notifications=NotificationsConfig(
            webhook_url="https://hooks.example.com/drift",
            severity_threshold="low",
        ),
    )
    error = urllib.error.URLError("connection refused")
    with patch("checkowners.notify.urllib.request.urlopen", side_effect=error):
        sent = send_notification(_drift_with(delta=0.9), config)
    assert sent is False


def test_send_notification_returns_false_on_os_error() -> None:
    config = Config(
        notifications=NotificationsConfig(
            webhook_url="https://hooks.example.com/drift",
            severity_threshold="low",
        ),
    )
    with patch("checkowners.notify.urllib.request.urlopen", side_effect=OSError("boom")):
        sent = send_notification(_drift_with(delta=0.9), config)
    assert sent is False


def test_send_notification_logs_warning_on_failure() -> None:
    config = Config(
        notifications=NotificationsConfig(
            webhook_url="https://hooks.example.com/drift",
            severity_threshold="low",
        ),
    )
    error = urllib.error.URLError("connection refused")
    with (
        patch("checkowners.notify.urllib.request.urlopen", side_effect=error),
        patch("checkowners.notify.logger.warning") as mock_warning,
    ):
        send_notification(_drift_with(delta=0.9), config)
    mock_warning.assert_called_once()


def test_send_notification_closes_response() -> None:
    config = Config(
        notifications=NotificationsConfig(
            webhook_url="https://hooks.example.com/drift",
            severity_threshold="low",
        ),
    )
    response = MagicMock()
    with patch("checkowners.notify.urllib.request.urlopen", return_value=response):
        sent = send_notification(_drift_with(delta=0.9), config)
    assert sent is True
    # The urlopen result is used as a context manager so it always closes.
    response.__enter__.assert_called_once()
    response.__exit__.assert_called_once()


def test_build_payload_basic() -> None:
    result = _drift_with(delta=0.6, bus_factor=2)
    config = Config()
    payload = _build_payload(result, "medium", config)
    assert payload["drift_detected"] is True
    assert payload["severity"] == "medium"
    assert payload["max_confidence_delta"] == 0.6
    assert payload["stale"][0]["path"] == "/src/main.py"
    assert payload["stale"][0]["bus_factor"] == 2
    assert "include_unchanged" not in payload


def test_build_payload_include_unchanged() -> None:
    result = _drift_with(delta=0.6)
    config = Config(notifications=NotificationsConfig(include_unchanged=True))
    payload = _build_payload(result, "medium", config)
    assert payload["include_unchanged"] is True


def test_send_notification_critical_signal_overrides_low_delta() -> None:
    config = Config(
        notifications=NotificationsConfig(
            webhook_url="https://hooks.example.com/drift",
            severity_threshold="critical",
        ),
    )
    drift = _drift_with(delta=0.05, bus_factor=1)
    captured: list[bytes] = []

    def _capture(req: urllib.request.Request, timeout: float) -> MagicMock:
        captured.append(req.data)
        return MagicMock()

    with patch("checkowners.notify.urllib.request.urlopen", side_effect=_capture):
        sent = send_notification(drift, config)
    assert sent is True
    body = json.loads(captured[0].decode("utf-8"))
    assert body["severity"] == "critical"
