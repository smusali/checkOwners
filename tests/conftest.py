"""Shared fixtures: keep every test away from the real ~/.checkowners."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from checkowners.github import set_offline


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point CHECKOWNERS_STATE_DIR at a per-test directory.

    The state file, graph cache, and email->handle cache all live under the
    state dir; without this fixture any test that exercises them would read
    and pollute the developer's real cache.
    """
    set_offline(False)
    monkeypatch.setenv("CHECKOWNERS_STATE_DIR", str(tmp_path / "checkowners-state"))


def _git_env(path: Path) -> dict[str, str]:
    return {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "HOME": str(path),
        "XDG_CONFIG_HOME": str(path),
    }


def init_git_repo(path: Path) -> Path:
    """Create a local git repo at ``path`` with signing disabled."""
    path.mkdir(parents=True, exist_ok=True)
    env = _git_env(path)
    subprocess.run(
        ["git", "init", "-b", "main"],  # noqa: S607
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    for key, value in (
        ("user.email", "test@example.com"),
        ("user.name", "Test"),
        ("commit.gpgsign", "false"),
    ):
        subprocess.run(  # noqa: S603
            ["git", "config", key, value],  # noqa: S607
            cwd=path,
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
    return path


def git_commit(
    path: Path,
    message: str,
    *,
    author: str,
    email: str,
    date: str,
) -> str:
    """Stage all changes and commit with pinned author and committer identity."""
    env = {
        **_git_env(path),
        "GIT_AUTHOR_NAME": author,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_AUTHOR_DATE": date,
        "GIT_COMMITTER_NAME": author,
        "GIT_COMMITTER_EMAIL": email,
        "GIT_COMMITTER_DATE": date,
    }
    subprocess.run(
        ["git", "add", "-A"],  # noqa: S607
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    subprocess.run(  # noqa: S603
        ["git", "commit", "-m", message],  # noqa: S607
        cwd=path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],  # noqa: S607
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.stdout.strip()
