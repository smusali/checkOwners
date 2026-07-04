"""Configuration loader for .github/checkowners.yml."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import yaml

from checkowners.models import (
    AnalysisConfig,
    BusFactorConfig,
    Config,
    DecayConfig,
    DriftConfig,
    DriftMode,
    GithubConfig,
    NotificationsConfig,
    OutputConfig,
    PathsConfig,
    ScoringConfig,
    Severity,
)

CONFIG_FILENAME = ".github/checkowners.yml"

#: Environment override for the config file location (used by the GitHub Action
#: to honor its ``config`` input). A relative value is resolved against the repo
#: root; an absolute value is used verbatim.
CONFIG_ENV_VAR = "CHECKOWNERS_CONFIG"

#: Environment override for ``drift.mode`` (used by the GitHub Action to honor
#: its ``mode`` input regardless of the committed checkowners.yml).
DRIFT_MODE_ENV_VAR = "CHECKOWNERS_DRIFT_MODE"

_CODEOWNERS_CANDIDATES: tuple[str, ...] = (
    ".github/CODEOWNERS",
    "CODEOWNERS",
    "docs/CODEOWNERS",
)

_DEFAULT_CODEOWNERS_PATH = ".github/CODEOWNERS"

_VALID_DRIFT_MODES: frozenset[str] = frozenset({"commit", "repo", "both"})
_VALID_SEVERITIES: frozenset[str] = frozenset({"low", "medium", "high", "critical"})


def find_codeowners_path(repo_root: Path) -> Path:
    """Locate existing CODEOWNERS file, checking all GitHub-supported locations."""
    for candidate in _CODEOWNERS_CANDIDATES:
        path = repo_root / candidate
        if path.exists():
            return path
    return repo_root / _DEFAULT_CODEOWNERS_PATH


def load_config(repo_root: Path | None = None) -> Config:
    """Load configuration, merging the YAML file with defaults and env overrides."""
    config_path = _resolve_config_path(repo_root)
    if not config_path.exists():
        return _apply_env_overrides(Config())
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if raw is None:
        return _apply_env_overrides(Config())
    if not isinstance(raw, dict):
        msg = f"Invalid checkowners config: expected a YAML mapping, got {type(raw).__name__}"
        raise ValueError(msg)
    return _apply_env_overrides(_merge_config(raw))


def _resolve_config_path(repo_root: Path | None) -> Path:
    root = repo_root if repo_root is not None else Path.cwd()
    override = os.environ.get(CONFIG_ENV_VAR)
    if override:
        override_path = Path(override)
        return override_path if override_path.is_absolute() else root / override_path
    return root / CONFIG_FILENAME


def _apply_env_overrides(config: Config) -> Config:
    """Apply environment-variable overrides on top of the loaded config."""
    mode_override = os.environ.get(DRIFT_MODE_ENV_VAR)
    if not mode_override:
        return config
    if mode_override not in _VALID_DRIFT_MODES:
        msg = (
            f"Invalid {DRIFT_MODE_ENV_VAR}: {mode_override!r}; "
            f"expected one of {sorted(_VALID_DRIFT_MODES)}"
        )
        raise ValueError(msg)
    return replace(config, drift=replace(config.drift, mode=cast(DriftMode, mode_override)))


def _merge_config(raw: dict[str, Any]) -> Config:
    builders: dict[str, tuple[str, Any]] = {
        "analysis": ("analysis", _build_analysis_config),
        "scoring": ("scoring", _build_scoring_config),
        "decay": ("decay", _build_decay_config),
        "bus_factor": ("bus_factor", _build_bus_factor_config),
        "paths": ("paths", _build_paths_config),
        "output": ("output", _build_output_config),
        "drift": ("drift", _build_drift_config),
        "notifications": ("notifications", _build_notifications_config),
        "github": ("github", _build_github_config),
    }
    kwargs: dict[str, Any] = {}
    for key, (field_name, builder) in builders.items():
        section = raw.get(key)
        if isinstance(section, dict):
            kwargs[field_name] = builder(section)
    return Config(**kwargs)


def _build_analysis_config(data: dict[str, Any]) -> AnalysisConfig:
    kwargs: dict[str, Any] = {}
    if "lookback_days" in data:
        kwargs["lookback_days"] = int(data["lookback_days"])
    if "min_commits" in data:
        kwargs["min_commits"] = int(data["min_commits"])
    if "top_n_owners" in data:
        kwargs["top_n_owners"] = int(data["top_n_owners"])
    if "confidence_threshold" in data:
        kwargs["confidence_threshold"] = float(data["confidence_threshold"])
    if "exclude_bots" in data:
        kwargs["exclude_bots"] = bool(data["exclude_bots"])
    return AnalysisConfig(**kwargs)


def _build_scoring_config(data: dict[str, Any]) -> ScoringConfig:
    kwargs: dict[str, Any] = {}
    if "recency_half_life_days" in data:
        kwargs["recency_half_life_days"] = int(data["recency_half_life_days"])
    if "recency_weight" in data:
        kwargs["recency_weight"] = float(data["recency_weight"])
    if "frequency_weight" in data:
        kwargs["frequency_weight"] = float(data["frequency_weight"])
    if "blame_weight" in data:
        kwargs["blame_weight"] = float(data["blame_weight"])
    if "review_weight" in data:
        kwargs["review_weight"] = float(data["review_weight"])
    return ScoringConfig(**kwargs)


def _build_decay_config(data: dict[str, Any]) -> DecayConfig:
    kwargs: dict[str, Any] = {}
    if "threshold_days" in data:
        kwargs["threshold_days"] = int(data["threshold_days"])
    if "alert_on_decay" in data:
        kwargs["alert_on_decay"] = bool(data["alert_on_decay"])
    return DecayConfig(**kwargs)


def _build_bus_factor_config(data: dict[str, Any]) -> BusFactorConfig:
    kwargs: dict[str, Any] = {}
    if "critical_threshold" in data:
        kwargs["critical_threshold"] = int(data["critical_threshold"])
    if "warn_threshold" in data:
        kwargs["warn_threshold"] = int(data["warn_threshold"])
    return BusFactorConfig(**kwargs)


def _build_paths_config(data: dict[str, Any]) -> PathsConfig:
    kwargs: dict[str, Any] = {}
    if "exclude" in data and isinstance(data["exclude"], list):
        kwargs["exclude"] = tuple(str(item) for item in data["exclude"])
    return PathsConfig(**kwargs)


def _build_output_config(data: dict[str, Any]) -> OutputConfig:
    kwargs: dict[str, Any] = {}
    if "header" in data:
        kwargs["header"] = str(data["header"])
    if "include_unowned" in data:
        kwargs["include_unowned"] = bool(data["include_unowned"])
    if "include_confidence" in data:
        kwargs["include_confidence"] = bool(data["include_confidence"])
    if "consolidate" in data:
        kwargs["consolidate"] = bool(data["consolidate"])
    return OutputConfig(**kwargs)


def _build_drift_config(data: dict[str, Any]) -> DriftConfig:
    kwargs: dict[str, Any] = {}
    if "mode" in data:
        mode_str = str(data["mode"])
        if mode_str not in _VALID_DRIFT_MODES:
            msg = f"Invalid drift.mode: {mode_str!r}; expected one of {sorted(_VALID_DRIFT_MODES)}"
            raise ValueError(msg)
        kwargs["mode"] = cast(DriftMode, mode_str)
    if "compare_to" in data:
        kwargs["compare_to"] = str(data["compare_to"])
    if "min_confidence_delta" in data:
        kwargs["min_confidence_delta"] = float(data["min_confidence_delta"])
    return DriftConfig(**kwargs)


def _expand_env_ref(raw: str) -> str:
    """Expand a ``${VAR}`` reference from the environment; pass literals through.

    Lets a committed checkowners.yml point at a secret without storing it, e.g.
    ``webhook_url: ${CHECKOWNERS_WEBHOOK_URL}``. An unset variable expands to "".
    """
    stripped = raw.strip()
    if stripped.startswith("${") and stripped.endswith("}"):
        var = stripped[2:-1]
        if var.isidentifier():
            return os.environ.get(var, "")
    return raw


def _build_notifications_config(data: dict[str, Any]) -> NotificationsConfig:
    kwargs: dict[str, Any] = {}
    if "webhook_url" in data:
        kwargs["webhook_url"] = _expand_env_ref(str(data["webhook_url"]))
    if "include_unchanged" in data:
        kwargs["include_unchanged"] = bool(data["include_unchanged"])
    if "severity_threshold" in data:
        sev_str = str(data["severity_threshold"])
        if sev_str not in _VALID_SEVERITIES:
            msg = (
                f"Invalid notifications.severity_threshold: {sev_str!r}; "
                f"expected one of {sorted(_VALID_SEVERITIES)}"
            )
            raise ValueError(msg)
        kwargs["severity_threshold"] = cast(Severity, sev_str)
    return NotificationsConfig(**kwargs)


def _build_github_config(data: dict[str, Any]) -> GithubConfig:
    if "token" in data:
        msg = (
            "github.token is not accepted in checkowners.yml: this file is "
            "typically committed to git, so storing a token there leaks the "
            "secret. Set the GITHUB_TOKEN environment variable instead."
        )
        raise ValueError(msg)
    kwargs: dict[str, Any] = {}
    if "org" in data:
        kwargs["org"] = str(data["org"])
    if "resolve_handles" in data:
        kwargs["resolve_handles"] = bool(data["resolve_handles"])
    if "resolve_teams" in data:
        kwargs["resolve_teams"] = bool(data["resolve_teams"])
    if "api_enabled" in data:
        kwargs["api_enabled"] = bool(data["api_enabled"])
    return GithubConfig(**kwargs)
