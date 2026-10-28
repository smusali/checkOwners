from __future__ import annotations

from pathlib import Path

import pytest

from benchmarks.harness import peak_rss, regression_failures, self_check, variance_failures
from checkowners.github import note_metric


def _sample(**overrides: float | int) -> dict[str, object]:
    payload: dict[str, object] = {
        "git_commands": 10,
        "git_commands_warm": 4,
        "git_commands_incremental": 6,
        "api_requests": 0,
        "cold_seconds": 2.0,
        "warm_seconds": 1.0,
        "incremental_seconds": 1.2,
    }
    payload.update(overrides)
    return payload


def test_variance_accepts_a_close_pair() -> None:
    left = _sample()
    right = _sample(cold_seconds=2.2, warm_seconds=1.1, incremental_seconds=1.3)
    assert variance_failures(left, right, variance=0.15) == []


def test_variance_rejects_a_different_command_count_and_a_slow_run() -> None:
    left = _sample()
    assert variance_failures(left, _sample(git_commands=11), variance=0.15)
    assert variance_failures(left, _sample(cold_seconds=3.0), variance=0.15)


def test_slowed_sample_fails_the_regression_check() -> None:
    baseline: dict[str, object] = {"ci": {"cold_seconds": 2.0}}
    assert regression_failures({"cold_seconds": 2.0}, baseline, factor=1.15) == []
    slowed = regression_failures({"cold_seconds": 2.31}, baseline, factor=1.15)
    assert slowed


def test_self_check_rejects_the_slowed_sample() -> None:
    assert self_check() == 0


def test_peak_rss_reads_macos_and_linux_timers() -> None:
    assert peak_rss("      4096  maximum resident set size\n") == 4096
    text = "Maximum resident set size (kbytes): 3\n"
    assert peak_rss(text) == 3072


def test_metrics_hook_stays_closed_until_configured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CHECKOWNERS_METRICS_PATH", raising=False)
    note_metric("git")
    target = tmp_path / "metrics.txt"
    monkeypatch.setenv("CHECKOWNERS_METRICS_PATH", str(target))
    note_metric("git")
    note_metric("api")
    assert target.read_text(encoding="utf-8") == "git\napi\n"
