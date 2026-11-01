"""The shared fixtures, re-exported from the modules that define them.

A fixture imported here is registered for every test, so a test module can
keep importing what it needs from `tests.conftest`.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from hypothesis import settings

from checkowners.github import set_offline
from checkowners.patterns import clear_pattern_cache
from tests.apiseam import (
    Call,
    FakeApi,
    Handler,
    Route,
    fake_api,
    reply,
)
from tests.builders import (
    DRIFT_DETECTED,
    EMPTY_OWNERSHIP,
    NO_DRIFT,
    NOW,
    OLD,
    OWNERSHIP,
    RECENT,
    declared_ownership,
    drift_config,
    owner_entry,
    owner_handles,
    ownership_map,
    readable_state,
    scoring_config,
    store_analysis,
    stored_ownership,
    write_codeowners,
    write_raw_state,
)
from tests.clirunner import (
    Cli,
    Payload,
    cli,
    seed_history,
)
from tests.gitrepo import (
    ALICE,
    BOB,
    DEFAULT_COMMIT_DATE,
    FIRST_DATE,
    REGRESSION_AS_OF,
    SECOND_DATE,
    THIRD_DATE,
    GitRepo,
    analyze_regression_repo,
    git_commit,
    git_repo,
    init_git_repo,
    regression_analysis_config,
    repo,
    script_regression_repo,
)

__all__ = [
    "ALICE",
    "BOB",
    "DEFAULT_COMMIT_DATE",
    "DRIFT_DETECTED",
    "EMPTY_OWNERSHIP",
    "FIRST_DATE",
    "NOW",
    "NO_DRIFT",
    "OLD",
    "OWNERSHIP",
    "RECENT",
    "REGRESSION_AS_OF",
    "SECOND_DATE",
    "THIRD_DATE",
    "Call",
    "Cli",
    "FakeApi",
    "GitRepo",
    "Handler",
    "Payload",
    "Route",
    "analyze_regression_repo",
    "cli",
    "declared_ownership",
    "drift_config",
    "fake_api",
    "git_commit",
    "git_repo",
    "init_git_repo",
    "owner_entry",
    "owner_handles",
    "ownership_map",
    "readable_state",
    "regression_analysis_config",
    "reply",
    "repo",
    "scoring_config",
    "script_regression_repo",
    "seed_history",
    "store_analysis",
    "stored_ownership",
    "write_codeowners",
    "write_raw_state",
]

settings.register_profile("ci", max_examples=200, derandomize=True, deadline=None)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "ci"))


@pytest.fixture(autouse=True)
def _clear_pattern_cache() -> None:
    """Drop compiled patterns so each test uses the current translator."""
    clear_pattern_cache()


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point CHECKOWNERS_STATE_DIR at a per-test directory.

    The state file, graph cache, and email->handle cache all live under the
    state dir; without this fixture any test that exercises them would read
    and pollute the developer's real cache.
    """
    set_offline(False)
    monkeypatch.setenv("CHECKOWNERS_STATE_DIR", str(tmp_path / "checkowners-state"))
