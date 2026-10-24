"""Tests running the Windows path-test corpus against patterns and path exclusions.

Validates:
- How pattern_matches() and match_path() behave on raw Windows-shaped paths
  vs POSIX-normalized paths.
- How paths.exclude filtering behaves with backslashes and mixed slashes.
- Deterministic behavior across any operating system runner (Linux, macOS, Windows).
"""

from __future__ import annotations

import pytest

from checkowners.analyze import _is_excluded
from checkowners.patterns import CodeownersRule, match_path, parse_rules, pattern_matches
from tests.windows_path_corpus import (
    WINDOWS_PATH_EXCLUDE_CORPUS,
    WINDOWS_PATH_MATCH_CORPUS,
    WindowsPathExcludeCase,
    WindowsPathMatchCase,
    normalize_posix_path,
)


@pytest.mark.parametrize(
    "case",
    WINDOWS_PATH_MATCH_CORPUS,
    ids=lambda c: f"{c.category}:{c.pattern}->{c.raw_path}",
)
def test_windows_path_matcher_corpus(case: WindowsPathMatchCase) -> None:
    # Direct matching without normalization
    direct_match = pattern_matches(case.pattern, case.raw_path)
    assert direct_match is case.expected_direct, (
        f"Direct match mismatch for {case.description}: "
        f"got {direct_match}, expected {case.expected_direct}"
    )

    # Normalized matching (simulating ingestion through git / posix normalization layer)
    normalized = normalize_posix_path(case.raw_path)
    normalized_match = pattern_matches(case.pattern, normalized)
    assert normalized_match is case.expected_normalized, (
        f"Normalized match mismatch for {case.description} (normalized '{normalized}'): "
        f"got {normalized_match}, expected {case.expected_normalized}"
    )


@pytest.mark.parametrize(
    "case",
    WINDOWS_PATH_EXCLUDE_CORPUS,
    ids=lambda c: f"{c.category}:{c.raw_path}",
)
def test_windows_path_exclude_corpus(case: WindowsPathExcludeCase) -> None:
    excluded = _is_excluded(case.raw_path, case.patterns)
    assert excluded is case.expected_exclude, (
        f"Exclude mismatch for {case.description}: "
        f"got {excluded}, expected {case.expected_exclude}"
    )


def test_codeowners_rule_evaluation_with_windows_paths() -> None:
    """Validate full multi-rule last-match-wins evaluation on normalized Windows paths."""
    codeowners_content = """
    # Infrastructure & Docs
    * @core-team
    docs/ @docs-team
    src/backend/ @backend-team
    src/backend/auth/ @security-team
    """
    rules = parse_rules(codeowners_content)

    test_samples = [
        (r"docs\setup.md", ("@docs-team",)),
        (r"src\backend\database.py", ("@backend-team",)),
        (r"src\backend\auth\jwt.py", ("@security-team",)),
        (r"root_config.json", ("@core-team",)),
    ]

    for raw_win_path, expected_owners in test_samples:
        normalized = normalize_posix_path(raw_win_path)
        winning_rule = match_path(rules, normalized)
        assert winning_rule is not None, f"Expected match for {raw_win_path}"
        assert winning_rule.owners == expected_owners, (
            f"Expected {expected_owners} for {raw_win_path}, got {winning_rule.owners}"
        )
