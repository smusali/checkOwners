"""Running the real CLI against a seeded repository, and reading its JSON."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from checkowners.cli import app
from tests.gitrepo import ALICE, BOB, GitRepo


@dataclass(frozen=True)
class Cli:
    """One checkowners CLI bound to a real git repository.

    `repo` is the checkout the commands run in. `run` invokes the CLI there
    and returns the Click result.
    """

    repo: GitRepo
    runner: CliRunner

    def run(self, *args: str) -> Result:
        """Invoke the CLI with `args` inside the repository."""
        return self.runner.invoke(app, list(args))

    def json(self, *args: str) -> Payload:
        """Invoke the CLI with `args` plus `--json` and return the parsed payload."""
        result = self.run(*args, "--json")
        assert result.exit_code in {0, 3}, result.output
        loaded: object = json.loads(result.stdout)
        assert isinstance(loaded, dict), result.stdout
        return Payload(loaded)

    def codeowners(self, text: str) -> Path:
        """Write `text` to the repository's CODEOWNERS and return its path."""
        target = self.repo.path / ".github" / "CODEOWNERS"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target


def seed_history(repo: GitRepo) -> None:
    """Commit a two-author history with pinned dates, enough to score owners."""
    repo.commit_files(
        {"src/app.py": "def total(items):\n    return sum(items)\n", "docs/guide.md": "# Guide\n"},
        "add the app",
        author="Alice",
        email=ALICE,
        date="2026-05-01T12:00:00+00:00",
    )
    repo.commit_files(
        {"src/api.py": "ROUTES = []\n"},
        "add the api",
        author="Bob",
        email=BOB,
        date="2026-05-10T12:00:00+00:00",
    )
    repo.commit_files(
        {"src/app.py": "def total(items):\n    return sum(items) or 0\n"},
        "guard the total",
        author="Alice",
        email=ALICE,
        date="2026-05-20T12:00:00+00:00",
    )


@pytest.fixture
def cli(git_repo: GitRepo, monkeypatch: pytest.MonkeyPatch) -> Cli:
    """Return a CLI bound to a seeded repository, with the process cwd inside it."""
    seed_history(git_repo)
    monkeypatch.chdir(git_repo.path)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    return Cli(repo=git_repo, runner=CliRunner())


class Payload:
    """A command's JSON payload, read with the type each field actually has.

    Every reader raises AssertionError when the field is missing or has
    another type, so a test states the shape it expects instead of narrowing
    `object` at each use.
    """

    def __init__(self, data: Mapping[str, object]) -> None:
        self.data = dict(data)

    def __contains__(self, key: str) -> bool:
        return key in self.data

    def __getitem__(self, key: str) -> object:
        return self.data[key]

    def text(self, key: str) -> str:
        """Return `key` as a string."""
        value = self.data[key]
        assert isinstance(value, str), f"{key}={value!r}"
        return value

    def number(self, key: str) -> float:
        """Return `key` as a float."""
        value = self.data[key]
        assert not isinstance(value, bool), f"{key}={value!r}"
        assert isinstance(value, int | float), f"{key}={value!r}"
        return float(value)

    def whole(self, key: str) -> int:
        """Return `key` as an integer."""
        value = self.data[key]
        assert not isinstance(value, bool), f"{key}={value!r}"
        assert isinstance(value, int), f"{key}={value!r}"
        return value

    def flag(self, key: str) -> bool:
        """Return `key` as a boolean."""
        value = self.data[key]
        assert isinstance(value, bool), f"{key}={value!r}"
        return value

    def child(self, key: str) -> Payload:
        """Return `key` as a nested payload."""
        value = self.data[key]
        assert isinstance(value, dict), f"{key}={value!r}"
        return Payload(value)

    def rows(self, key: str) -> list[Payload]:
        """Return `key` as a list of nested payloads."""
        value = self.data[key]
        assert isinstance(value, list), f"{key}={value!r}"
        return [Payload(item) for item in value if isinstance(item, dict)]

    def strings(self, key: str) -> list[str]:
        """Return `key` as a list of strings."""
        value = self.data[key]
        assert isinstance(value, list), f"{key}={value!r}"
        return [item for item in value if isinstance(item, str)]
