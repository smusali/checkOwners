"""Configuration loader for .github/checkowners.yml."""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any, TypeGuard, get_args

import yaml

from checkowners.models import (
    AnalysisConfig,
    BusFactorConfig,
    Config,
    DecayConfig,
    DriftConfig,
    DriftMode,
    FindingRule,
    GitConfig,
    GithubConfig,
    IdentityMode,
    ModelVersions,
    OutputConfig,
    PathsConfig,
    PolicyConfig,
    PrivacyConfig,
    QualificationConfig,
    QualificationStrategy,
    RiskConfig,
    ScoringConfig,
    Suppression,
    models_payload,
)

CONFIG_FILENAME = ".github/checkowners.yml"

#: Environment override for the config file location (used by the GitHub Action
#: to honor its ``config`` input). A relative value is resolved against the repo
#: root; an absolute value is used verbatim.
CONFIG_ENV_VAR = "CHECKOWNERS_CONFIG"

#: Environment override for ``drift.mode`` (used by the GitHub Action to honor
#: its ``mode`` input regardless of the committed checkowners.yml).
DRIFT_MODE_ENV_VAR = "CHECKOWNERS_DRIFT_MODE"

#: Environment override for ``drift.baseline_file`` (used by the GitHub Action
#: to honor its ``baseline`` input).
BASELINE_ENV_VAR = "CHECKOWNERS_BASELINE"

_CODEOWNERS_CANDIDATES: tuple[str, ...] = (
    ".github/CODEOWNERS",
    "CODEOWNERS",
    "docs/CODEOWNERS",
)

_DEFAULT_CODEOWNERS_PATH = ".github/CODEOWNERS"

_VALID_DRIFT_MODES: frozenset[str] = frozenset(get_args(DriftMode))
_VALID_QUALIFICATION_STRATEGIES: frozenset[str] = frozenset(get_args(QualificationStrategy))
_VALID_FINDING_RULES: frozenset[str] = frozenset(get_args(FindingRule))

SUPPORTED_CONFIG_VERSIONS: frozenset[int] = frozenset({1})

_V2_TOP_LEVEL: frozenset[str] = frozenset(
    {
        "version",
        "analysis",
        "qualification",
        "scoring",
        "decay",
        "bus_factor",
        "risk",
        "paths",
        "output",
        "drift",
        "github",
        "git",
        "suppressions",
        "model",
        "identity",
        "bots",
        "policy",
        "privacy",
        "contributors",
        "criticality",
    }
)

_V2_ANALYSIS: frozenset[str] = frozenset(
    {
        "lookback_days",
        "min_commits",
        "top_n_owners",
        "max_owners",
        "confidence_threshold",
        "exclude_bots",
        "respect_gitattributes",
        "max_runtime_seconds",
        "max_git_workers",
        "max_api_requests",
    }
)
_V2_QUALIFICATION: frozenset[str] = frozenset({"strategy", "min_commits", "strong_blame_override"})
_V2_SCORING: frozenset[str] = frozenset(
    {
        "recency_half_life_days",
        "recency_weight",
        "frequency_weight",
        "blame_weight",
        "review_weight",
        "recency_reliability",
        "frequency_reliability",
        "blame_reliability",
        "review_reliability",
    }
)
_V2_DECAY: frozenset[str] = frozenset({"threshold_days", "alert_on_decay"})
_V2_BUS_FACTOR: frozenset[str] = frozenset({"critical_threshold", "warn_threshold"})
_V2_RISK: frozenset[str] = frozenset({"truck_factor_thresholds"})
_V2_PATHS: frozenset[str] = frozenset({"exclude"})
_V2_OUTPUT: frozenset[str] = frozenset(
    {
        "header",
        "include_unowned",
        "include_confidence",
        "consolidate",
        "max_bytes",
        "verify_round_trip",
        "allow_broad_patterns",
        "anonymize",
        "aggregate_only",
    }
)
_V2_DRIFT: frozenset[str] = frozenset(
    {"mode", "min_confidence_delta", "hysteresis_runs", "baseline_file"}
)
_V2_GITHUB: frozenset[str] = frozenset(
    {"org", "resolve_handles", "resolve_teams", "api_enabled", "token"}
)
_V2_GIT: frozenset[str] = frozenset(
    {
        "blame_ignore_revs_file",
        "detect_moves",
        "mass_refactor_file_fraction",
    }
)
_V2_IDENTITY: frozenset[str] = frozenset({"mailmap", "mode"})
_V2_PRIVACY: frozenset[str] = frozenset({"redact_emails"})
_V2_CONTRIBUTORS: frozenset[str] = frozenset({"exclude"})
_IDENTITY_MODES: frozenset[str] = frozenset(get_args(IdentityMode))
_V2_POLICY: frozenset[str] = frozenset({"incomplete_analysis"})
_V2_INCOMPLETE: frozenset[str] = frozenset({"fail"})
_V2_BOTS: frozenset[str] = frozenset({"exclude"})
_V2_MODEL: frozenset[str] = frozenset({"ownership", "risk", "topology", "signals"})
_V2_SIGNALS: frozenset[str] = frozenset({"recency", "frequency", "blame", "reviews"})

_SIGNAL_FIELDS: dict[str, dict[str, str]] = {
    "recency": {
        "weight": "recency_weight",
        "half_life_days": "recency_half_life_days",
        "reliability": "recency_reliability",
    },
    "frequency": {
        "weight": "frequency_weight",
        "reliability": "frequency_reliability",
    },
    "blame": {
        "weight": "blame_weight",
        "reliability": "blame_reliability",
    },
    "reviews": {
        "weight": "review_weight",
        "reliability": "review_reliability",
    },
}

_V2_SECTIONS: dict[str, frozenset[str]] = {
    "qualification": _V2_QUALIFICATION,
    "scoring": _V2_SCORING,
    "decay": _V2_DECAY,
    "bus_factor": _V2_BUS_FACTOR,
    "risk": _V2_RISK,
    "paths": _V2_PATHS,
    "output": _V2_OUTPUT,
    "drift": _V2_DRIFT,
    "github": _V2_GITHUB,
    "git": _V2_GIT,
    "identity": _V2_IDENTITY,
    "policy": _V2_POLICY,
    "privacy": _V2_PRIVACY,
    "contributors": _V2_CONTRIBUTORS,
}


def _is_drift_mode(value: str) -> TypeGuard[DriftMode]:
    return value in _VALID_DRIFT_MODES


def _is_qualification_strategy(value: str) -> TypeGuard[QualificationStrategy]:
    return value in _VALID_QUALIFICATION_STRATEGIES


def _is_finding_rule(value: str) -> TypeGuard[FindingRule]:
    return value in _VALID_FINDING_RULES


def _is_identity_mode(value: str) -> TypeGuard[IdentityMode]:
    return value in _IDENTITY_MODES


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
    prepared, models = _prepare_config(raw)
    return _apply_env_overrides(replace(_merge_config(prepared), models=models))


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
    if mode_override:
        if not _is_drift_mode(mode_override):
            msg = (
                f"Invalid {DRIFT_MODE_ENV_VAR}: {mode_override!r}; "
                f"expected one of {sorted(_VALID_DRIFT_MODES)}"
            )
            raise ValueError(msg)
        config = replace(config, drift=replace(config.drift, mode=mode_override))
    baseline_override = os.environ.get(BASELINE_ENV_VAR, "").strip()
    if baseline_override:
        config = replace(config, drift=replace(config.drift, baseline_file=baseline_override))
    return config


_Prepared = tuple[dict[str, Any], ModelVersions]


def _prepare_config(raw: dict[str, Any]) -> _Prepared:
    _config_version(raw)
    return _translate_v2(raw), _pinned_models(raw)


def _config_version(raw: dict[str, Any]) -> int:
    if "version" not in raw:
        msg = "checkowners config requires version: 1"
        raise ValueError(msg)
    value = raw["version"]
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value in SUPPORTED_CONFIG_VERSIONS
    ):
        return value
    supported = ", ".join(str(item) for item in sorted(SUPPORTED_CONFIG_VERSIONS))
    msg = f"Unsupported checkowners config version {value!r}; this release supports {supported}"
    raise ValueError(msg)


def _reject_unknown_keys(block: dict[str, Any], allowed: frozenset[str], prefix: str) -> None:
    for key in block:
        if isinstance(key, str) and key in allowed:
            continue
        name = f"{prefix}.{key}" if prefix else str(key)
        msg = f"Unsupported checkowners config key: {name}"
        raise ValueError(msg)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_mapping(value: object, key: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        msg = f"{key} must be a mapping"
        raise ValueError(msg)
    return value


def _translate_v2(raw: dict[str, Any]) -> dict[str, Any]:
    _reject_unknown_keys(raw, _V2_TOP_LEVEL, "")
    translated = dict(raw)
    translated.pop("version", None)
    for name, allowed in _V2_SECTIONS.items():
        if name not in translated:
            continue
        section = _require_mapping(translated[name], name)
        _reject_unknown_keys(section, allowed, name)
    if translated.get("suppressions") is None:
        translated.pop("suppressions", None)
    elif "suppressions" in translated and not isinstance(translated["suppressions"], list):
        msg = "suppressions must be a list"
        raise ValueError(msg)
    _validate_policy(translated.get("policy"))
    translated["analysis"] = _translate_analysis(translated.get("analysis"), translated.get("bots"))
    translated.pop("bots", None)
    scoring = _translate_scoring(translated.get("scoring"), translated.get("model"))
    translated.pop("model", None)
    if scoring is not None:
        translated["scoring"] = scoring
    if translated.get("analysis") is None:
        translated.pop("analysis", None)
    return translated


def _validate_policy(policy: object) -> None:
    if policy is None:
        return
    section = _require_mapping(policy, "policy")
    incomplete = section.get("incomplete_analysis")
    if incomplete is None:
        return
    block = _require_mapping(incomplete, "policy.incomplete_analysis")
    _reject_unknown_keys(block, _V2_INCOMPLETE, "policy.incomplete_analysis")
    if "fail" in block and not isinstance(block["fail"], bool):
        msg = "policy.incomplete_analysis.fail must be a boolean"
        raise ValueError(msg)


def _translate_analysis(analysis: object, bots: object) -> dict[str, Any] | None:
    if analysis is None and bots is None:
        return None
    section = dict(_require_mapping(analysis, "analysis")) if analysis is not None else {}
    if analysis is not None:
        _reject_unknown_keys(section, _V2_ANALYSIS, "analysis")
        if "lookback_days" in section and not _is_int(section["lookback_days"]):
            msg = "analysis.lookback_days must be an integer; adaptive lookback is not supported"
            raise ValueError(msg)
        if "max_owners" in section:
            max_owners = section.pop("max_owners")
            if not _is_int(max_owners):
                msg = "analysis.max_owners must be an integer"
                raise ValueError(msg)
            if "top_n_owners" in section and section["top_n_owners"] != max_owners:
                msg = "analysis.max_owners and analysis.top_n_owners disagree"
                raise ValueError(msg)
            section["top_n_owners"] = max_owners
    if bots is not None:
        bot_section = _require_mapping(bots, "bots")
        _reject_unknown_keys(bot_section, _V2_BOTS, "bots")
        if "exclude" in bot_section:
            exclude = bot_section["exclude"]
            if not isinstance(exclude, bool):
                msg = "bots.exclude must be a boolean"
                raise ValueError(msg)
            if "exclude_bots" in section and section["exclude_bots"] != exclude:
                msg = "bots.exclude and analysis.exclude_bots disagree"
                raise ValueError(msg)
            section["exclude_bots"] = exclude
    return section


def _translate_scoring(scoring: object, model: object) -> dict[str, Any] | None:
    if model is not None:
        model_section = _require_mapping(model, "model")
        _reject_unknown_keys(model_section, _V2_MODEL, "model")
    else:
        model_section = {}
    if scoring is None:
        merged: dict[str, Any] = {}
    else:
        merged = dict(_require_mapping(scoring, "scoring"))
    signals = model_section.get("signals")
    if signals is None:
        return merged or None
    signal_section = _require_mapping(signals, "model.signals")
    _reject_unknown_keys(signal_section, _V2_SIGNALS, "model.signals")
    for signal_name, fields in _SIGNAL_FIELDS.items():
        if signal_name not in signal_section:
            continue
        block = _require_mapping(signal_section[signal_name], f"model.signals.{signal_name}")
        _reject_unknown_keys(block, frozenset(fields), f"model.signals.{signal_name}")
        for source, destination in fields.items():
            if source not in block:
                continue
            if destination in merged and merged[destination] != block[source]:
                msg = f"model.signals.{signal_name}.{source} and scoring.{destination} disagree"
                raise ValueError(msg)
            merged[destination] = block[source]
    return merged or None


def _pinned_models(raw: dict[str, Any]) -> ModelVersions:
    model = raw.get("model")
    if model is None:
        return ModelVersions()
    section = _require_mapping(model, "model")
    implemented = models_payload()
    pins: dict[str, str] = {}
    for name, current in implemented.items():
        if name not in section:
            continue
        value = section[name]
        if value != current:
            msg = f"model.{name} {value!r} is not implemented; this release implements {current}"
            raise ValueError(msg)
        pins[name] = current
    return ModelVersions(
        ownership=pins.get("ownership", implemented["ownership"]),
        risk=pins.get("risk", implemented["risk"]),
        topology=pins.get("topology", implemented["topology"]),
    )


def _merge_config(raw: dict[str, Any]) -> Config:
    builders: dict[str, tuple[str, Any]] = {
        "analysis": ("analysis", _build_analysis_config),
        "scoring": ("scoring", _build_scoring_config),
        "decay": ("decay", _build_decay_config),
        "bus_factor": ("bus_factor", _build_bus_factor_config),
        "risk": ("risk", _build_risk_config),
        "paths": ("paths", _build_paths_config),
        "output": ("output", _build_output_config),
        "drift": ("drift", _build_drift_config),
        "github": ("github", _build_github_config),
        "git": ("git", _build_git_config),
        "policy": ("policy", _build_policy_config),
    }
    kwargs: dict[str, Any] = {}
    for key, (field_name, builder) in builders.items():
        section = raw.get(key)
        if isinstance(section, dict):
            kwargs[field_name] = builder(section)
    analysis_raw = _mapping_section(raw, "analysis")
    qualification_raw = _mapping_section(raw, "qualification")
    analysis = kwargs.get("analysis", AnalysisConfig())
    qualification = _build_qualification_config(qualification_raw)
    min_commits = _resolve_min_commits(analysis_raw, qualification_raw)
    kwargs["analysis"] = replace(analysis, min_commits=min_commits)
    kwargs["qualification"] = replace(qualification, min_commits=min_commits)
    identity = _mapping_section(raw, "identity")
    kwargs["git"] = _apply_identity_mailmap(kwargs.get("git", GitConfig()), identity)
    kwargs["identity_mode"] = _build_identity_mode(identity)
    kwargs["privacy"] = _build_privacy_config(_mapping_section(raw, "privacy"))
    kwargs["contributors_exclude"] = _build_contributors_exclude(raw.get("contributors"))
    if "suppressions" in raw:
        kwargs["suppressions"] = _build_suppressions(raw["suppressions"])
    if "criticality" in raw:
        kwargs["criticality"] = _build_criticality(raw["criticality"])
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
    if "respect_gitattributes" in data:
        kwargs["respect_gitattributes"] = bool(data["respect_gitattributes"])
    for key in ("max_runtime_seconds", "max_git_workers", "max_api_requests"):
        if key in data:
            kwargs[key] = _require_positive_int(data[key], f"analysis.{key}")
    return AnalysisConfig(**kwargs)


def _require_positive_int(value: object, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        msg = f"{key} must be a positive integer"
        raise ValueError(msg)
    return value


def _build_policy_config(data: dict[str, Any]) -> PolicyConfig:
    incomplete = data.get("incomplete_analysis")
    if not isinstance(incomplete, dict) or "fail" not in incomplete:
        return PolicyConfig()
    fail = incomplete["fail"]
    if not isinstance(fail, bool):
        msg = "policy.incomplete_analysis.fail must be a boolean"
        raise ValueError(msg)
    return PolicyConfig(incomplete_analysis_fail=fail)


def _mapping_section(raw: dict[str, Any], key: str) -> dict[str, Any]:
    section = raw.get(key)
    return section if isinstance(section, dict) else {}


def _resolve_min_commits(
    analysis_raw: dict[str, Any],
    qualification_raw: dict[str, Any],
) -> int:
    if "min_commits" in qualification_raw:
        return int(qualification_raw["min_commits"])
    if "min_commits" in analysis_raw:
        return int(analysis_raw["min_commits"])
    return AnalysisConfig().min_commits


def _build_qualification_config(data: dict[str, Any]) -> QualificationConfig:
    kwargs: dict[str, Any] = {}
    if "strategy" in data:
        strategy = str(data["strategy"])
        if not _is_qualification_strategy(strategy):
            msg = (
                f"Invalid qualification.strategy: {strategy!r}; "
                f"expected one of {sorted(_VALID_QUALIFICATION_STRATEGIES)}"
            )
            raise ValueError(msg)
        kwargs["strategy"] = strategy
    if "min_commits" in data:
        kwargs["min_commits"] = int(data["min_commits"])
    if "strong_blame_override" in data:
        kwargs["strong_blame_override"] = float(data["strong_blame_override"])
    return QualificationConfig(**kwargs)


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
    if "recency_reliability" in data:
        kwargs["recency_reliability"] = float(data["recency_reliability"])
    if "frequency_reliability" in data:
        kwargs["frequency_reliability"] = float(data["frequency_reliability"])
    if "blame_reliability" in data:
        kwargs["blame_reliability"] = float(data["blame_reliability"])
    if "review_reliability" in data:
        kwargs["review_reliability"] = float(data["review_reliability"])
    return ScoringConfig(**kwargs)


def _build_decay_config(data: dict[str, Any]) -> DecayConfig:
    kwargs: dict[str, Any] = {}
    if "threshold_days" in data:
        kwargs["threshold_days"] = int(data["threshold_days"])
    if "alert_on_decay" in data:
        kwargs["alert_on_decay"] = bool(data["alert_on_decay"])
    return DecayConfig(**kwargs)


def _build_risk_config(data: dict[str, Any]) -> RiskConfig:
    raw = data.get("truck_factor_thresholds")
    if raw is None:
        return RiskConfig()
    if not isinstance(raw, list) or len(raw) != 3:
        msg = "risk.truck_factor_thresholds must be three increasing numbers in (0, 1]"
        raise ValueError(msg)
    low = _require_unit_interval(raw[0])
    mid = _require_unit_interval(raw[1])
    high = _require_unit_interval(raw[2])
    if not low < mid < high:
        msg = "risk.truck_factor_thresholds must be three increasing numbers in (0, 1]"
        raise ValueError(msg)
    return RiskConfig(truck_factor_thresholds=(low, mid, high))


def _build_criticality(value: object) -> tuple[tuple[str, float], ...]:
    section = _require_mapping(value, "criticality")
    rules: list[tuple[str, float]] = []
    for pattern, weight in section.items():
        if not isinstance(pattern, str) or not pattern.strip():
            msg = "criticality patterns must be non-empty strings"
            raise ValueError(msg)
        rules.append((pattern, _require_open_unit(weight, f"criticality.{pattern}")))
    return tuple(rules)


def _require_open_unit(value: object, key: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        msg = f"{key} must be a number in (0, 1]"
        raise ValueError(msg)
    number = float(value)
    if number <= 0 or number > 1:
        msg = f"{key} must be a number in (0, 1]"
        raise ValueError(msg)
    return number


def _require_unit_interval(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        msg = "risk.truck_factor_thresholds must be three increasing numbers in (0, 1]"
        raise ValueError(msg)
    number = float(value)
    if number <= 0 or number > 1:
        msg = "risk.truck_factor_thresholds must be three increasing numbers in (0, 1]"
        raise ValueError(msg)
    return number


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
    if "max_bytes" in data:
        kwargs["max_bytes"] = int(data["max_bytes"])
    if "verify_round_trip" in data:
        kwargs["verify_round_trip"] = bool(data["verify_round_trip"])
    if "allow_broad_patterns" in data:
        kwargs["allow_broad_patterns"] = bool(data["allow_broad_patterns"])
    if "anonymize" in data:
        kwargs["anonymize"] = _require_bool(data["anonymize"], "output.anonymize")
    if "aggregate_only" in data:
        kwargs["aggregate_only"] = _require_bool(data["aggregate_only"], "output.aggregate_only")
    return OutputConfig(**kwargs)


def _require_bool(value: object, key: str) -> bool:
    if not isinstance(value, bool):
        msg = f"{key} must be a boolean"
        raise ValueError(msg)
    return value


def _build_identity_mode(identity: dict[str, Any]) -> IdentityMode:
    if "mode" not in identity:
        return "handle"
    mode = identity["mode"]
    if not isinstance(mode, str) or not _is_identity_mode(mode):
        msg = f"Invalid identity.mode: {mode!r}; expected one of {sorted(_IDENTITY_MODES)}"
        raise ValueError(msg)
    return mode


def _build_privacy_config(data: dict[str, Any]) -> PrivacyConfig:
    if "redact_emails" not in data:
        return PrivacyConfig()
    return PrivacyConfig(
        redact_emails=_require_bool(data["redact_emails"], "privacy.redact_emails"),
    )


def _build_contributors_exclude(contributors: object) -> tuple[str, ...]:
    if contributors is None:
        return ()
    section = _require_mapping(contributors, "contributors")
    if "exclude" not in section:
        return ()
    excluded = section["exclude"]
    if not isinstance(excluded, list):
        msg = "contributors.exclude must be a list"
        raise ValueError(msg)
    names: list[str] = []
    for item in excluded:
        if not isinstance(item, str) or not item.strip():
            msg = "contributors.exclude entries must be non-empty strings"
            raise ValueError(msg)
        names.append(item.strip())
    return tuple(names)


def _build_drift_config(data: dict[str, Any]) -> DriftConfig:
    kwargs: dict[str, Any] = {}
    if "mode" in data:
        mode_str = str(data["mode"])
        if not _is_drift_mode(mode_str):
            msg = f"Invalid drift.mode: {mode_str!r}; expected one of {sorted(_VALID_DRIFT_MODES)}"
            raise ValueError(msg)
        kwargs["mode"] = mode_str
    if "min_confidence_delta" in data:
        kwargs["min_confidence_delta"] = float(data["min_confidence_delta"])
    if "hysteresis_runs" in data:
        runs = int(data["hysteresis_runs"])
        if runs < 1:
            msg = f"Invalid drift.hysteresis_runs: {runs!r}; expected an integer >= 1"
            raise ValueError(msg)
        kwargs["hysteresis_runs"] = runs
    if "baseline_file" in data:
        kwargs["baseline_file"] = str(data["baseline_file"])
    return DriftConfig(**kwargs)


def _build_suppressions(raw: object) -> tuple[Suppression, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        msg = "Invalid suppressions: expected a YAML list"
        raise ValueError(msg)
    return tuple(_build_suppression(item, index) for index, item in enumerate(raw))


def _build_suppression(data: object, index: int) -> Suppression:
    prefix = f"suppressions[{index}]"
    if not isinstance(data, dict):
        msg = f"Invalid {prefix}: expected a mapping"
        raise ValueError(msg)
    if "path" not in data:
        msg = f"Invalid {prefix}: path is required"
        raise ValueError(msg)
    if "rule" not in data:
        msg = f"Invalid {prefix}: rule is required"
        raise ValueError(msg)
    if "reason" not in data:
        msg = f"Invalid {prefix}: reason is required"
        raise ValueError(msg)
    path = str(data["path"]).strip()
    rule = str(data["rule"]).strip()
    reason = str(data["reason"]).strip()
    if not path:
        msg = f"Invalid {prefix}: path is required"
        raise ValueError(msg)
    if not rule:
        msg = f"Invalid {prefix}: rule is required"
        raise ValueError(msg)
    if not reason:
        msg = f"Invalid {prefix}: reason is required"
        raise ValueError(msg)
    if not _is_finding_rule(rule):
        msg = f"Invalid {prefix}.rule: {rule!r}; expected one of {sorted(_VALID_FINDING_RULES)}"
        raise ValueError(msg)
    expires: date | None = None
    raw_expires = data.get("expires")
    if raw_expires is not None and str(raw_expires).strip():
        try:
            expires = date.fromisoformat(str(raw_expires))
        except ValueError:
            msg = f"Invalid {prefix}.expires: {raw_expires!r}; expected YYYY-MM-DD"
            raise ValueError(msg) from None
    return Suppression(path=path, rule=rule, reason=reason, expires=expires)


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


def _build_git_config(data: dict[str, Any]) -> GitConfig:
    kwargs: dict[str, Any] = {}
    if "blame_ignore_revs_file" in data:
        kwargs["blame_ignore_revs_file"] = str(data["blame_ignore_revs_file"])
    if "detect_moves" in data:
        kwargs["detect_moves"] = bool(data["detect_moves"])
    if "mass_refactor_file_fraction" in data:
        fraction = float(data["mass_refactor_file_fraction"])
        if fraction < 0.0 or fraction > 1.0:
            msg = (
                f"Invalid git.mass_refactor_file_fraction: {fraction!r}; "
                "expected a number in [0, 1]"
            )
            raise ValueError(msg)
        kwargs["mass_refactor_file_fraction"] = fraction
    return GitConfig(**kwargs)


def _apply_identity_mailmap(git: GitConfig, identity: dict[str, Any]) -> GitConfig:
    if "mailmap" not in identity:
        return git
    return replace(git, use_mailmap=bool(identity["mailmap"]))
