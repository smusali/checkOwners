"""Rejecting a bad configuration file: unknown keys and invalid values."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from checkowners.config import (
    load_config,
)
from tests.test_config import (
    _write_config,
)


def test_rename_similarity_stays_between_1_and_100(tmp_path: Path) -> None:
    root = _write_config(
        tmp_path,
        "analysis:\n  follow_renames: false\n  rename_similarity: 80\n",
    )
    cfg = load_config(repo_root=root)
    assert cfg.analysis.follow_renames is False
    assert cfg.analysis.rename_similarity == 80
    invalid = _write_config(tmp_path, "analysis:\n  rename_similarity: 0\n")
    with pytest.raises(ValueError, match=r"analysis\.rename_similarity"):
        load_config(repo_root=invalid)
    too_high = _write_config(tmp_path, "analysis:\n  rename_similarity: 101\n")
    with pytest.raises(ValueError, match=r"1 to 100"):
        load_config(repo_root=too_high)
    bad_flag = _write_config(tmp_path, "analysis:\n  follow_renames: 1\n")
    with pytest.raises(ValueError, match=r"analysis\.follow_renames must be a boolean"):
        load_config(repo_root=bad_flag)


def test_load_config_invalid_yaml(tmp_path: Path) -> None:
    root = _write_config(tmp_path, "[[[invalid yaml")
    with pytest.raises(yaml.YAMLError):
        load_config(repo_root=root)


def test_load_config_non_mapping(tmp_path: Path) -> None:
    root = _write_config(tmp_path, "- a\n- b\n")
    with pytest.raises(ValueError, match="expected a YAML mapping"):
        load_config(repo_root=root)


@pytest.mark.parametrize(
    "content",
    [
        "risk:\n  truck_factor_thresholds: [0.5, 0.9]\n",
        "risk:\n  truck_factor_thresholds: [0.9, 0.5, 0.75]\n",
        "risk:\n  truck_factor_thresholds: [0, 0.5, 0.9]\n",
        "risk:\n  truck_factor_thresholds: [0.5, 0.75, 1.1]\n",
        "risk:\n  truck_factor_thresholds: no\n",
        "risk:\n  truck_factor_thresholds: [true, 0.6, 0.8]\n",
        "risk:\n  truck_factor_thresholds: ['x', 0.6, 0.8]\n",
    ],
)
def test_risk_thresholds_rejected(tmp_path: Path, content: str) -> None:
    root = _write_config(tmp_path, content)
    with pytest.raises(ValueError, match="truck_factor_thresholds"):
        load_config(repo_root=root)


def test_load_config_unknown_keys_rejected(tmp_path: Path) -> None:
    root = _write_config(tmp_path, "custom_field: true\nanother: 42\n")
    with pytest.raises(ValueError, match="Unsupported checkowners config key"):
        load_config(repo_root=root)


def test_block_order_invalid_rejected(tmp_path: Path) -> None:
    root = _write_config(tmp_path, "output:\n  block_order: sideways\n")
    with pytest.raises(ValueError, match=r"Invalid output\.block_order"):
        load_config(repo_root=root)
    root = _write_config(tmp_path, "output:\n  block_order: 1\n")
    with pytest.raises(ValueError, match=r"Invalid output\.block_order"):
        load_config(repo_root=root)


def test_drift_mode_invalid_rejected(tmp_path: Path) -> None:
    root = _write_config(tmp_path, "drift:\n  mode: nonsense\n")
    with pytest.raises(ValueError, match=r"Invalid drift\.mode"):
        load_config(repo_root=root)


def test_hysteresis_runs_invalid_rejected(tmp_path: Path) -> None:
    root = _write_config(tmp_path, "drift:\n  hysteresis_runs: 0\n")
    with pytest.raises(ValueError, match=r"Invalid drift\.hysteresis_runs"):
        load_config(repo_root=root)


def test_drift_mode_env_override_invalid_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHECKOWNERS_DRIFT_MODE", "sideways")
    with pytest.raises(ValueError, match="Invalid CHECKOWNERS_DRIFT_MODE"):
        load_config(repo_root=tmp_path)


def test_load_config_github_token_rejected(tmp_path: Path) -> None:
    root = _write_config(tmp_path, "github:\n  token: ghp_secret\n")
    with pytest.raises(ValueError, match=r"github\.token is not accepted"):
        load_config(repo_root=root)


def test_qualification_strategy_invalid_rejected(tmp_path: Path) -> None:
    root = _write_config(tmp_path, "qualification:\n  strategy: fuzzy\n")
    with pytest.raises(ValueError, match=r"Invalid qualification\.strategy"):
        load_config(repo_root=root)


def test_git_mass_refactor_fraction_invalid_rejected(tmp_path: Path) -> None:
    root = _write_config(tmp_path, "git:\n  mass_refactor_file_fraction: 1.5\n")
    with pytest.raises(ValueError, match=r"Invalid git\.mass_refactor_file_fraction"):
        load_config(repo_root=root)
    weight = _write_config(tmp_path, "git:\n  co_author_weight: 1.5\n")
    with pytest.raises(ValueError, match=r"Invalid git\.co_author_weight"):
        load_config(repo_root=weight)
    strategy = _write_config(tmp_path, "git:\n  merge_strategy: fast-forward\n")
    with pytest.raises(ValueError, match=r"Invalid git\.merge_strategy"):
        load_config(repo_root=strategy)
    numbered = _write_config(tmp_path, "git:\n  merge_strategy: 1\n")
    with pytest.raises(ValueError, match=r"Invalid git\.merge_strategy"):
        load_config(repo_root=numbered)
    flag = _write_config(tmp_path, "git:\n  count_co_authors: 1\n")
    with pytest.raises(ValueError, match=r"git\.count_co_authors must be a boolean"):
        load_config(repo_root=flag)
    boolean_weight = _write_config(tmp_path, "git:\n  co_author_weight: true\n")
    with pytest.raises(ValueError, match=r"Invalid git\.co_author_weight"):
        load_config(repo_root=boolean_weight)
    text_weight = _write_config(tmp_path, "git:\n  co_author_weight: high\n")
    with pytest.raises(ValueError, match=r"Invalid git\.co_author_weight"):
        load_config(repo_root=text_weight)
    negative = _write_config(tmp_path, "git:\n  co_author_weight: -0.1\n")
    with pytest.raises(ValueError, match=r"Invalid git\.co_author_weight"):
        load_config(repo_root=negative)


def test_leftover_version_key_is_rejected(tmp_path: Path) -> None:
    root = _write_config(tmp_path, "version: 1\nanalysis:\n  lookback_days: 10\n")
    with pytest.raises(ValueError, match="Unsupported checkowners config key: version"):
        load_config(repo_root=root)


def test_leftover_model_pin_is_rejected(tmp_path: Path) -> None:
    root = _write_config(tmp_path, "model:\n  ownership: leftover\n")
    with pytest.raises(ValueError, match=r"Unsupported checkowners config key: model\.ownership"):
        load_config(repo_root=root)


@pytest.mark.parametrize(
    ("content", "match"),
    [
        ("version: 1\n", "Unsupported checkowners config key: version"),
        ("analysis:\n  lookback_days: forever\n", "lookback_days"),
        (
            "model:\n  signals:\n    recency:\n      strategy: rolling\n",
            "recency_strategy",
        ),
        (
            "model:\n  signals:\n    recency:\n      half_life_floor_days: 90\n"
            "      half_life_ceiling_days: 90\n",
            "below",
        ),
        (
            "model:\n  signals:\n    historical_depth:\n      weight: 0.1\n",
            "historical_depth",
        ),
    ],
)
def test_unknown_config_keys_rejected(tmp_path: Path, content: str, match: str) -> None:
    root = _write_config(tmp_path, content)
    with pytest.raises(ValueError, match=match):
        load_config(repo_root=root)


@pytest.mark.parametrize(
    ("content", "match"),
    [
        ("criticality: []\n", "criticality must be a mapping"),
        ("criticality:\n  '': 0.5\n", "non-empty"),
        ("criticality:\n  'docs/**': 0\n", r"criticality\.docs/"),
        ("criticality:\n  'docs/**': true\n", r"criticality\.docs/"),
        ("analysis: []\n", "analysis must be a mapping"),
        ("suppressions: {}\n", "suppressions must be a list"),
        ("bots:\n  exclude: 1\n", "bots.exclude must be a boolean"),
        ("policy:\n  drift:\n    severity: critical\n", "severity"),
        ("policy:\n  drift:\n    severity: 1\n", "severity"),
        ("security:\n  paths: auth/**\n", "security.paths"),
        ("security:\n  paths:\n    - ''\n", "security.paths"),
        ("security:\n  paths:\n    - '  '\n", "security.paths"),
        ('output:\n  anonymize: "yes"\n', "output.anonymize"),
        ("output:\n  aggregate_only: 1\n", "output.aggregate_only"),
        ('privacy:\n  redact_emails: "yes"\n', "privacy.redact_emails"),
        ("identity:\n  mode: 1\n", "identity.mode"),
        ("contributors: []\n", "contributors must be a mapping"),
        ("contributors:\n  exclude: alice\n", "contributors.exclude must be a list"),
        ("contributors:\n  exclude:\n    - '  '\n", "non-empty strings"),
        ("contributors:\n  exclude:\n    - 1\n", "non-empty strings"),
        ("analysis:\n  max_owners: 4\n", "Unsupported checkowners config key: analysis.max_owners"),
        (
            "analysis:\n  exclude_bots: true\n",
            "Unsupported checkowners config key: analysis.exclude_bots",
        ),
        ("scoring:\n  review_weight: 0.2\n", "Unsupported checkowners config key: scoring"),
    ],
)
def test_config_rejects_bad_section_values(tmp_path: Path, content: str, match: str) -> None:
    root = _write_config(tmp_path, content)
    with pytest.raises(ValueError, match=match):
        load_config(repo_root=root)


@pytest.mark.parametrize(
    ("content", "match"),
    [
        ("analysis:\n  max_runtime_seconds: 0\n", "positive integer"),
        ("analysis:\n  max_git_workers: -1\n", "positive integer"),
        ("analysis:\n  max_api_requests: false\n", "positive integer"),
    ],
)
def test_budget_keys_rejected(tmp_path: Path, content: str, match: str) -> None:
    root = _write_config(tmp_path, content)
    with pytest.raises(ValueError, match=match):
        load_config(repo_root=root)


def test_suppression_builder_rejects_non_lists(tmp_path: Path) -> None:
    root = _write_config(tmp_path, "suppressions: {}\n")
    with pytest.raises(ValueError, match="suppressions must be a list"):
        load_config(repo_root=root)


@pytest.mark.parametrize(
    ("content", "match"),
    [
        ("identity:\n  negative_cache_ttl_days: true\n", "non-negative integer"),
        ("identity:\n  negative_cache_ttl_days: -1\n", "non-negative integer"),
        ("identity:\n  do_not_merge: nope\n", "must be a list"),
        ("identity:\n  do_not_merge:\n    - [only]\n", "must be pairs"),
        ("identity:\n  do_not_merge:\n    - [1, 2]\n", "non-empty strings"),
        ("identity:\n  do_not_merge:\n    - [' ', bob]\n", "non-empty strings"),
        ("bots:\n  additional: nope\n", "must be a list"),
        ("bots:\n  additional:\n    - ' '\n", "non-empty strings"),
        ("identities: []\n", "identities"),
        ("identities:\n  ' ':\n    emails: []\n", "non-empty strings"),
        ("identities:\n  ada: []\n", "identities.ada"),
        ("identities:\n  ada:\n    extra: 1\n", "Unsupported"),
        ("identities:\n  ada:\n    service_account: nope\n", "service_account"),
        ("identities:\n  ada:\n    emails: nope\n", "must be a list"),
        ("contributors:\n  roster: []\n", "must be a mapping"),
        ("contributors:\n  roster:\n    ' ': former\n", "non-empty strings"),
        ("contributors:\n  roster:\n    ada: 1\n", "Invalid contributors.roster"),
        ("contributors:\n  roster:\n    ada: nope\n", "Invalid contributors.roster"),
    ],
)
def test_identity_config_rejects_invalid_values(tmp_path: Path, content: str, match: str) -> None:
    root = _write_config(tmp_path, content)
    with pytest.raises(ValueError, match=match):
        load_config(repo_root=root)


@pytest.mark.parametrize(
    "content",
    [
        "drift:\n  severity_thresholds: 1\n",
        "drift:\n  severity_thresholds: [0.1, 0.2]\n",
        "drift:\n  severity_thresholds: [true, 0.2, 0.3, 0.4]\n",
        "drift:\n  severity_thresholds: ['a', 0.2, 0.3, 0.4]\n",
        "drift:\n  severity_thresholds: [-0.1, 0.2, 0.3, 0.4]\n",
        "drift:\n  severity_thresholds: [0.1, 0.2, 0.3, 1.1]\n",
        "drift:\n  severity_thresholds: [0.4, 0.3, 0.2, 0.1]\n",
        "drift:\n  severity_thresholds: [0.2, 0.2, 0.3, 0.4]\n",
    ],
)
def test_severity_thresholds_must_be_four_ascending_numbers(tmp_path: Path, content: str) -> None:
    root = _write_config(tmp_path, content)
    with pytest.raises(ValueError, match="four ascending numbers"):
        load_config(repo_root=root)
